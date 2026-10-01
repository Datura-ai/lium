#!/usr/bin/env python3
"""Time warm commands on an already-rented pod, against a local sshd instead of a real pod.

What it stands up, all on 127.0.0.1 and all torn down at exit:

- an OpenSSH ``sshd`` running as the current user (no root), with throwaway host and client keys;
- a TCP proxy in front of it that holds every chunk for half of ``--rtt-ms`` in each direction,
  so a handshake costs its real number of round trips, and that counts SSH connections;
- a mock Lium API that answers ``GET /pods`` with one pod pointing at the proxy (after
  ``--api-ms``), accepts ``POST /pods/<id>/schedule-removal``, and counts requests.

Then it runs, with the interpreter given by ``--python`` (a checkout's ``.venv/bin/python``,
so the same script times two checkouts):

- ``cli``: ``lium exec bench-pod true`` ``--runs`` times, each a new process, like a shell or an agent;
- ``sdk``: one ``Lium()`` and ``--runs`` calls of ``Lium.exec(pod, command="true")``;
- ``machine``: ``--runs`` calls of a ``@lium.machine(keep_warm=...)`` function on the warm pod.

and prints, per mode, the median wall time per command, SSH connections opened and API
requests made. ``--json`` prints the same as one JSON object.

Only stdlib here. Needs ``sshd`` and ``ssh-keygen`` on PATH or in /usr/sbin.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import http.server
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections import Counter
from pathlib import Path
from typing import IO, Dict, List, Optional

POD_ID = "0b6a7a3e-3797-4d2e-9d3e-5ea7c0ffee01"
POD_NAME = "bench-pod"


def find_sshd() -> Optional[str]:
    return shutil.which("sshd") or next((p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.path.exists(p)), None)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalSshd:
    """``sshd -D`` on a free port, accepting only a throwaway ed25519 key, for the current user."""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.port = free_port()
        self.user = getpass.getuser()
        self.client_key = workdir / "client_ed25519"
        self._proc: Optional[subprocess.Popen] = None
        self._log: Optional[IO[str]] = None

    def start(self) -> "LocalSshd":
        sshd = find_sshd()
        if not sshd:
            raise RuntimeError("sshd not found")
        host_key = self.workdir / "host_ed25519"
        for key in (host_key, self.client_key):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        authorized = self.workdir / "authorized_keys"
        authorized.write_text(Path(f"{self.client_key}.pub").read_text())
        config = self.workdir / "sshd_config"
        config.write_text(textwrap.dedent(f"""\
            Port {self.port}
            ListenAddress 127.0.0.1
            HostKey {host_key}
            PidFile {self.workdir / "sshd.pid"}
            AuthorizedKeysFile {authorized}
            PasswordAuthentication no
            KbdInteractiveAuthentication no
            UsePAM no
            StrictModes no
            MaxStartups 100
            MaxSessions 100
            Subsystem sftp internal-sftp
            """))
        self._log = open(self.workdir / "sshd.log", "w")
        self._proc = subprocess.Popen(
            [sshd, "-D", "-e", "-f", str(config)],
            stdout=subprocess.DEVNULL, stderr=self._log,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                if self._proc.poll() is not None:
                    raise RuntimeError(f"sshd exited: {(self.workdir / 'sshd.log').read_text()}")
                time.sleep(0.05)
        raise RuntimeError("sshd did not start listening")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._log:
            self._log.close()
            self._log = None


class DelayProxy:
    """Forward 127.0.0.1:<port> to ``target_port``, delaying each chunk by ``one_way_s`` per direction."""

    def __init__(self, target_port: int, one_way_s: float):
        self.target_port = target_port
        self.one_way_s = one_way_s
        self.port = free_port()
        self.connections = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "DelayProxy":
        self._thread.start()
        self._ready.wait(5)
        return self

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", self.port))
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            server.close()

    async def _handle(self, client_reader, client_writer) -> None:
        self.connections += 1
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            client_writer.close()
            return
        await asyncio.gather(
            self._pipe(client_reader, upstream_writer),
            self._pipe(upstream_reader, client_writer),
            return_exceptions=True,
        )
        for writer in (client_writer, upstream_writer):
            writer.close()

    async def _pipe(self, reader, writer) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        async def receive():
            while True:
                data = await reader.read(65536)
                await queue.put((loop.time() + self.one_way_s, data))
                if not data:
                    return

        async def send():
            while True:
                due, data = await queue.get()
                wait = due - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                if not data:
                    try:
                        writer.write_eof()
                    except (OSError, RuntimeError):
                        pass  # the peer already closed its side, so there is nothing left to half-close
                    return
                writer.write(data)
                await writer.drain()

        try:
            await asyncio.gather(receive(), send())
        except (ConnectionError, OSError):
            pass  # the other side hung up; the caller closes both ends

    def stop(self) -> None:
        async def cancel_and_stop():
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._loop.stop()

        asyncio.run_coroutine_threadsafe(cancel_and_stop(), self._loop)
        self._thread.join(5)


def pod_row(ssh_port: int, user: str, name: str = POD_NAME, pod_id: str = POD_ID) -> Dict:
    return {
        "id": pod_id,
        "pod_name": name,
        "status": "RUNNING",
        "ssh_connect_cmd": f"ssh {user}@127.0.0.1 -p {ssh_port}",
        "ports_mapping": {},
        "created_at": "2026-09-23T00:00:00Z",
        "updated_at": "2026-09-23T00:00:00Z",
        "price": 0.5,
        "gpu_count": 1,
        "template": {},
        "executor": {
            "id": "11111111-3797-4d2e-9d3e-5ea7c0ffee02",
            "machine_name": "NVIDIA GeForce RTX 4090",
            "price_per_hour": 0.5,
            "specs": {"gpu": {"count": 1, "details": [{"name": "NVIDIA GeForce RTX 4090"}]}},
            "location": {"country": "Local"},
        },
    }


class MockApi:
    """The few Lium API routes a warm command touches, each answered after ``latency_s``."""

    def __init__(self, pods: List[Dict], latency_s: float):
        self.pods = pods
        self.latency_s = latency_s
        self.requests: Counter = Counter()
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status: int, body) -> None:
                time.sleep(api.latency_s)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                api.requests[f"GET {path}"] += 1
                if path == "/pods":
                    return self._answer(200, api.pods)
                if path == "/version":
                    return self._answer(200, {"version": "bench", "features": []})
                return self._answer(404, {"detail": "not found"})

            def do_POST(self):
                path = self.path.split("?")[0]
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                api.requests[f"POST {path.replace(POD_ID, '{id}')}"] += 1
                return self._answer(200, {"ok": True})

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "MockApi":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()


class Bench:
    """sshd + delay proxy + mock API, and the environment a ``lium`` process needs to use them."""

    def __init__(self, rtt_ms: float, api_ms: float):
        self._tmp = tempfile.mkdtemp(prefix="lium-bench-")
        self.workdir = Path(self._tmp)
        self.home = self.workdir / "home"
        self.home.mkdir()
        self.sshd = LocalSshd(self.workdir).start()
        self.proxy = DelayProxy(self.sshd.port, rtt_ms / 2000).start()
        self.api = MockApi([pod_row(self.proxy.port, self.sshd.user)], api_ms / 1000).start()

    def env(self, **extra: str) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LIUM_", "VIRTUAL_ENV", "PYTHONPATH"))}
        env.pop("FORCE_COLOR", None)
        env.update(
            HOME=str(self.home),
            NO_COLOR="1",
            LIUM_API_KEY="bench-not-a-real-key",
            LIUM_BASE_URL=self.api.url,
            LIUM_SSH_KEY_PATH=str(self.sshd.client_key),
            LIUM_TELEMETRY="0",
        )
        env.update(extra)
        return env

    def counters(self):
        return self.proxy.connections, sum(self.api.requests.values()), dict(self.api.requests)

    def reset_counters(self) -> None:
        self.proxy.connections = 0
        self.api.requests.clear()

    def close(self) -> None:
        # a persistent ssh control master started by `lium exec` belongs to this bench's HOME
        subprocess.run(["pkill", "-f", str(self.home)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.api.stop()
        self.proxy.stop()
        self.sshd.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)


CLI_MAIN = "import sys; from lium.cli.cli import main; sys.argv = ['lium'] + sys.argv[1:]; main()"


def bench_cli(bench: Bench, python: str, runs: int, extra_env: Dict[str, str]) -> Dict:
    env = bench.env(**extra_env)
    started = time.perf_counter()
    subprocess.run([python, "-c", "import lium.cli.cli"], env=env, check=True, cwd=bench.workdir)
    import_s = time.perf_counter() - started
    bench.reset_counters()
    times = []
    for _ in range(runs):
        started = time.perf_counter()
        proc = subprocess.run([python, "-c", CLI_MAIN, "exec", POD_NAME, "true"], env=env, cwd=bench.workdir,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        times.append(time.perf_counter() - started)
        if proc.returncode != 0:
            raise RuntimeError(f"lium exec failed ({proc.returncode}): {proc.stdout}\n{proc.stderr}")
    ssh, api, by_route = bench.counters()
    return {"runs": runs, "per_command_s": times, "median_s": statistics.median(times),
            "cli_import_s": import_s, "ssh_connections": ssh, "api_requests": api, "api_by_route": by_route}


SDK_SCRIPT = """
import json, sys, time
from lium.sdk import Lium
runs = int(sys.argv[1])
lium = Lium()
pod = next(p for p in lium.ps() if p.name == sys.argv[2])
times = []
for _ in range(runs):
    started = time.perf_counter()
    result = lium.exec(pod, command="true")
    times.append(time.perf_counter() - started)
    assert result["exit_code"] == 0, result
print(json.dumps(times))
"""


def bench_sdk(bench: Bench, python: str, runs: int, extra_env: Dict[str, str]) -> Dict:
    bench.reset_counters()
    proc = subprocess.run([python, "-c", SDK_SCRIPT, str(runs), POD_NAME], env=bench.env(**extra_env),
                          cwd=bench.workdir, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"sdk bench failed: {proc.stderr}")
    times = json.loads(proc.stdout.strip().splitlines()[-1])
    ssh, api, by_route = bench.counters()
    return {"runs": runs, "per_command_s": times, "median_s": statistics.median(times),
            "ssh_connections": ssh, "api_requests": api - 1, "api_by_route": by_route,
            "note": "api_requests excludes the one ps() that found the pod"}


MACHINE_SCRIPT = """
import json, sys, time
import lium
runs = int(sys.argv[1])

@lium.machine("1xRTX4090", keep_warm=300, quiet=True, timeout=120)
def add(a, b):
    return a + b

times = []
for i in range(runs):
    started = time.perf_counter()
    assert add(i, 1) == i + 1
    times.append(time.perf_counter() - started)
print(json.dumps(times))
"""


def bench_machine(bench: Bench, python: str, runs: int, extra_env: Dict[str, str]) -> Dict:
    """A warm ``@lium.machine`` call: the pod is found by name (``lium-fn-<key>``), never rented."""
    probe = subprocess.run(
        [python, "-c", "from lium.sdk.decorators import _warm_key; print(_warm_key('1xRTX4090', None))"],
        env=bench.env(**extra_env), cwd=bench.workdir, capture_output=True, text=True, check=True,
    )
    key = probe.stdout.strip()
    bench.api.pods = [pod_row(bench.proxy.port, bench.sshd.user, name=f"lium-fn-{key}")]
    script = bench.workdir / "machine_bench.py"   # @lium.machine reads the function's source from its file
    script.write_text(MACHINE_SCRIPT)
    bench.reset_counters()
    try:
        proc = subprocess.run([python, str(script), str(runs)], env=bench.env(**extra_env), cwd=bench.workdir,
                              capture_output=True, text=True, check=False)
    finally:
        bench.api.pods = [pod_row(bench.proxy.port, bench.sshd.user)]
    if proc.returncode != 0:
        raise RuntimeError(f"machine bench failed: {proc.stderr}")
    times = json.loads(proc.stdout.strip().splitlines()[-1])
    ssh, api, by_route = bench.counters()
    return {"runs": runs, "per_command_s": times, "median_s": statistics.median(times),
            "warm_median_s": statistics.median(times[1:]) if len(times) > 1 else times[0],
            "ssh_connections": ssh, "api_requests": api, "api_by_route": by_route}


MODES = {"cli": bench_cli, "sdk": bench_sdk, "machine": bench_machine}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--python", default=sys.executable, help="interpreter that has the lium checkout to time")
    parser.add_argument("--rtt-ms", type=float, default=150.0, help="round trip to the pod (default 150)")
    parser.add_argument("--api-ms", type=float, default=300.0,
                        help="time for one API request from a new process: connect + TLS + server (default 300)")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--modes", default="cli,sdk,machine")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                        help="extra environment for the timed processes (repeatable)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    extra_env = dict(item.split("=", 1) for item in args.env)

    bench = Bench(args.rtt_ms, args.api_ms)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    results = {"rtt_ms": args.rtt_ms, "api_ms": args.api_ms, "python": args.python}
    try:
        for mode in args.modes.split(","):
            results[mode] = MODES[mode](bench, args.python, args.runs, extra_env)
    finally:
        bench.close()

    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    print(f"pod RTT {args.rtt_ms:.0f} ms, API request {args.api_ms:.0f} ms, {args.runs} runs, {args.python}")
    for mode in args.modes.split(","):
        r = results[mode]
        each = " ".join(f"{t:.2f}" for t in r["per_command_s"])
        print(f"{mode:8s} median {r['median_s']:.2f} s  [{each}]  ssh connections {r['ssh_connections']}  "
              f"api requests {r['api_requests']} {r['api_by_route']}")
        if mode == "cli":
            print(f"         (python start + `import lium.cli.cli`: {r['cli_import_s']:.2f} s of each)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
