"""Background jobs on a pod: start detached, come back later, wait for a port, read the log.

A served model or a long training run outlives the SSH session that starts it and,
for an agent, the turn that starts it. The pieces every caller otherwise rebuilds by
hand — ``nohup setsid … < /dev/null &``, a PID file, an exit-code file, a
``/dev/tcp`` poll until the port answers — live here, behind one object.

Every job keeps five small files next to each other on the pod, named after the job::

    <job_dir>/<name>.log    stdout+stderr of the command
    <job_dir>/<name>.pid    PID of the job's process group leader
    <job_dir>/<name>.id     the pod's boot id and the process start time — pins that PID
    <job_dir>/<name>.exit   exit code, written when the command ends
    <job_dir>/<name>.cmd    the command as given

so :meth:`Lium.job` can re-attach to a running job from another process or a later
agent turn with nothing but the pod and the name. The ``.id`` file is what keeps a
PID that survived a pod restart on ``/workspace`` from being mistaken for the job:
``status()``, ``kill()`` and the launcher only treat the process as the job when its
boot id and start time (``/proc/<pid>/stat`` field 22) still match the file. No
``.id`` file (a job from before this file existed) is a mismatch too, not a reason to
trust the PID.
"""

from __future__ import annotations

import re
import shlex
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from .exceptions import LiumError
from .models import PodInfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Lium

DEFAULT_JOB_DIR = "/workspace/logs"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


# The lazy paramiko import: DAH-3053.
def _ssh_exception() -> type:
    """``paramiko.SSHException``, imported when an exec fails rather than with the CLI.

    The CLI imports with paramiko unloaded; ``client.py`` keeps it lazy
    and so does this module — by the time an exec raises, ``ssh_connection`` has
    imported it anyway.
    """
    import paramiko

    return paramiko.SSHException


def validate_job_name(name: str) -> str:
    """A job name is a file-name stem: letters, digits, ``_ . -``, at most 64 chars."""
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"Invalid job name {name!r}: use letters, digits, '_', '.' or '-' (max 64 chars, must start "
            "with a letter or digit)"
        )
    return name


def default_job_name() -> str:
    return time.strftime("job-%Y%m%dT%H%M%SZ", time.gmtime())


def job_paths(name: str, job_dir: str = DEFAULT_JOB_DIR) -> Dict[str, str]:
    stem = f"{job_dir.rstrip('/')}/{name}"
    return {
        "log_path": f"{stem}.log",
        "pid_file": f"{stem}.pid",
        "id_file": f"{stem}.id",
        "exit_file": f"{stem}.exit",
        "cmd_file": f"{stem}.cmd",
    }


def process_identity(pid: str) -> str:
    """Shell that prints what pins ``pid`` to one process: the boot id and the start time.

    ``pid`` is shell text (``$!``, ``4242`` or ``"$(cat file)"``). The start time is
    field 22 of ``/proc/<pid>/stat`` in clock ticks since boot; the comm field can hold
    spaces, so the line is cut after its closing parenthesis first. Both parts are empty
    when the process is gone, so a comparison with a recorded identity fails then too.
    """
    return (
        f"{{ cat /proc/sys/kernel/random/boot_id 2>/dev/null; "
        f"sed 's/.*) //' /proc/{pid}/stat 2>/dev/null | cut -d' ' -f20; }} | tr '\\n' ' '"
    )


def identity_matches(pid: str, id_file: str) -> str:
    """Shell condition: ``pid`` is still the process recorded in ``id_file``.

    A missing ``id_file`` is a mismatch, never a fall-back to the PID alone: a job
    started before the ``.id`` file existed cannot be told from a stale PID file, so
    ``status()`` reads it as ``gone``, ``kill()`` does not signal it and the launcher
    does not refuse a new start under its name. Such a job has to be started again
    with :meth:`Lium.run_background` to be tracked.
    """
    q = shlex.quote(id_file)
    return f'{{ [ -f {q} ] && [ "$({process_identity(pid)})" = "$(cat {q})" ]; }}'


def build_job_launcher(
    command: str,
    *,
    name: str,
    job_dir: str = DEFAULT_JOB_DIR,
    workdir: Optional[str] = None,
    prelude: str = "",
) -> str:
    """The remote command line that starts ``command`` as job ``name`` and prints its PID.

    The job runs under ``nohup setsid`` with stdin closed so the SSH channel's end
    does not reach it. A wrapper shell records the command's exit code in the
    ``.exit`` file when it ends, so a caller can tell "still running" from
    "finished" from "vanished" without guessing. Starting a second job under a
    name whose process is still alive is refused (exit 3) rather than silently
    running two servers; "alive" needs the recorded PID to match the ``.id`` file,
    so a name whose files predate the ``.id`` file is free to start.

    Environment values are not part of this line: :meth:`Lium.run_background`
    hands them to :meth:`Lium.exec`, which exports them over the session's stdin
    as one variable, and passes the ``prelude`` that applies them inside the job
    shell (after ``bash -lc``'s profile, ahead of ``workdir`` and the command). The
    ``.cmd`` file records the raw ``command`` only, so :meth:`Lium.job` hands back
    the same ``command`` :meth:`Lium.run_background` was given and no environment
    value is written to disk.
    """
    q = shlex.quote
    name = validate_job_name(name)  # safe to interpolate: [A-Za-z0-9_.-] only
    p = job_paths(name, job_dir)
    pid_file = q(p["pid_file"])
    inner = command if workdir is None else f"cd {q(workdir)} && {command}"
    inner = prelude + inner
    wrapper = f"bash -lc {q(inner)}; echo $? > {q(p['exit_file'])}"
    # "still running" needs the recorded PID to be the job's own process, not one that
    # took the number after a pod restart (the .pid file on /workspace survives one).
    recorded_pid = f'"$(cat {pid_file})"'
    still_running = (
        f"[ -f {pid_file} ] && kill -0 {recorded_pid} 2>/dev/null && {identity_matches(recorded_pid, p['id_file'])}"
    )
    return (
        f"mkdir -p {q(job_dir)} || exit 1; "
        f"if {still_running}; "
        f"then echo \"job {name} is still running (pid $(cat {pid_file}))\" >&2; exit 3; fi; "
        f"rm -f {q(p['exit_file'])} {q(p['id_file'])}; "
        f"printf %s {q(command)} > {q(p['cmd_file'])}; "
        f"nohup setsid bash -c {q(wrapper)} > {q(p['log_path'])} 2>&1 < /dev/null & "
        f"echo $! > {q(p['pid_file'])}; {process_identity('$!')} > {q(p['id_file'])}; echo $!"
    )


def build_status_probe(pid: int, exit_file: str, id_file: Optional[str] = None) -> str:
    """Prints ``exited <code>``, ``running`` or ``gone`` for a job.

    With ``id_file``, ``running`` also needs the live process to carry the recorded
    boot id and start time: after a pod restart the PID may belong to something else.
    When that ``.id`` file is missing on the pod, the probe reads ``gone`` even
    while the PID is alive.
    """
    q = shlex.quote
    alive = f"kill -0 {int(pid)} 2>/dev/null"
    if id_file is not None:
        alive += f" && {identity_matches(str(int(pid)), id_file)}"
    return (
        f"if [ -f {q(exit_file)} ]; then echo \"exited $(cat {q(exit_file)})\"; "
        f"elif {alive}; then echo running; else echo gone; fi"
    )


def build_port_probe(port: int, host: str = "127.0.0.1", connect_timeout: int = 3) -> str:
    """Prints ``port open`` or ``port closed`` for a TCP port as seen from inside the pod.

    Uses bash's ``/dev/tcp`` so it needs no ``nc``/``curl`` on the image; ``timeout``
    bounds a connect that is filtered rather than refused.
    """
    target = f"exec 3<>/dev/tcp/{host}/{int(port)}"
    return (
        f"if timeout {int(connect_timeout)} bash -c {shlex.quote(target)} 2>/dev/null; "
        f"then echo 'port open'; else echo 'port closed'; fi"
    )


def parse_status(stdout: str) -> Dict[str, Any]:
    """``exited 0`` → ``{"state": "exited", "exit_code": 0}``; ``running``/``gone`` likewise."""
    for line in stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("exited"):
            code = line.split(None, 1)[1].strip() if " " in line else ""
            return {"state": "exited", "exit_code": int(code) if code.lstrip("-").isdigit() else None}
        if line in ("running", "gone"):
            return {"state": line, "exit_code": None}
    return {"state": "unknown", "exit_code": None}


class Job:
    """A command started in the background on a pod.

    Returned by :meth:`Lium.run_background` and :meth:`Lium.job`. Every method
    opens one short SSH session; nothing is cached, so the answers reflect the
    pod as it is now.
    """

    def __init__(
        self,
        client: "Lium",
        pod: PodInfo,
        *,
        name: str,
        pid: int,
        command: str,
        job_dir: str = DEFAULT_JOB_DIR,
    ):
        self._client = client
        self.pod = pod
        self.name = validate_job_name(name)
        self.pid = int(pid)
        self.command = command
        self.job_dir = job_dir
        paths = job_paths(self.name, job_dir)
        self.log_path = paths["log_path"]
        self.pid_file = paths["pid_file"]
        self.id_file = paths["id_file"]
        self.exit_file = paths["exit_file"]
        self.cmd_file = paths["cmd_file"]

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Job(name={self.name!r}, pid={self.pid}, pod={self.pod.name or self.pod.huid!r}, log={self.log_path!r})"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "pid": self.pid,
            "command": self.command,
            "pod_id": self.pod.id,
            "pod_name": self.pod.name,
            "job_dir": self.job_dir,
            "log_path": self.log_path,
            "pid_file": self.pid_file,
            "id_file": self.id_file,
            "exit_file": self.exit_file,
        }

    # -- state -------------------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """``{"state": "running" | "exited" | "gone", "exit_code": int | None}``.

        ``gone`` means the process is not alive and left no exit code — killed as
        a group, or the pod restarted underneath it (a PID that another process
        took after the restart does not count as alive: the ``.id`` file decides).
        A job with no ``.id`` file (started before the file existed) is ``gone`` too.
        """
        result = self._client.exec(self.pod, command=self._status_probe(), timeout=30)
        return parse_status(result.get("stdout", ""))

    def _status_probe(self) -> str:
        return build_status_probe(self.pid, self.exit_file, self.id_file)

    def is_running(self) -> bool:
        return self.status()["state"] == "running"

    def poll(self) -> Optional[int]:
        """``None`` while the job runs, its exit code once it ended (``subprocess.Popen.poll`` semantics).

        Raises:
            LiumError: the process is gone without an exit code.
        """
        status = self.status()
        if status["state"] == "running":
            return None
        if status["state"] == "exited":
            return status["exit_code"]
        raise LiumError(f"Job {self.name} (pid {self.pid}) on pod {self._pod_label()} is gone without an exit code")

    def wait(self, timeout: Optional[float] = None, *, poll_interval: float = 5) -> int:
        """Block until the job ends and return its exit code.

        Raises:
            TimeoutError: still running after ``timeout`` seconds (the job keeps running).
            LiumError: the process vanished without writing an exit code.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Job {self.name} (pid {self.pid}) on pod {self._pod_label()} still running after {timeout}s; "
                    f"log: {self.log_path}"
                )
            time.sleep(poll_interval)

    def wait_for_port(
        self,
        port: int,
        timeout: float = 600,
        *,
        host: str = "127.0.0.1",
        poll_interval: float = 3,
    ) -> None:
        """Block until TCP ``port`` accepts connections inside the pod.

        The probe and the job's liveness are read in the same SSH round trip, and
        the job's state is judged first: a server that crashed while loading fails
        this call at once, with its exit code and the last log lines, instead of
        burning the whole timeout — even when another process holds the port. A
        job that exited 0 with the port open counts as ready (it forked its
        server and left).

        Raises:
            LiumError: the job ended (or vanished) before the port answered.
            TimeoutError: the port did not answer within ``timeout`` seconds.
        """
        probe = self._status_probe() + "; " + build_port_probe(port, host)
        deadline = time.monotonic() + timeout
        last_error: Optional[str] = None
        while True:
            try:
                stdout = self._client.exec(self.pod, command=probe, timeout=30).get("stdout", "")
                last_error = None
            except (OSError, LiumError, _ssh_exception()) as exc:  # SSH not reachable right now: keep polling
                stdout, last_error = "", str(exc)
            port_open = "port open" in stdout
            status = parse_status(stdout)
            # The job's state is read before the port: an open port is only this
            # job's if the job is alive (or exited 0 — a launcher that forked its
            # server and left). A dead job with the port held by something else
            # is a failure, not a ready server.
            if status["state"] == "exited":
                if status["exit_code"] == 0 and port_open:
                    return
                raise LiumError(
                    f"Job {self.name} exited with code {status['exit_code']} before port {port} answered "
                    f"on pod {self._pod_label()}"
                    + (" (the port is open, but not by this job)" if port_open else "")
                    + f".\nLast log lines ({self.log_path}):\n{self.logs(tail=40)}"
                )
            if status["state"] == "gone":
                raise LiumError(
                    f"Job {self.name} (pid {self.pid}) is gone before port {port} answered on pod "
                    f"{self._pod_label()}"
                    + (" (the port is open, but not by this job)" if port_open else "")
                    + f".\nLast log lines ({self.log_path}):\n{self.logs(tail=40)}"
                )
            if port_open:
                return
            if time.monotonic() >= deadline:
                why = f" (last SSH error: {last_error})" if last_error else ""
                raise TimeoutError(
                    f"Port {port} on pod {self._pod_label()} did not answer within {timeout}s{why}; "
                    f"job {self.name} is {status['state']}.\nLast log lines ({self.log_path}):\n{self.logs(tail=40)}"
                )
            time.sleep(poll_interval)

    # -- output and control ------------------------------------------------------------------

    def logs(self, tail: Optional[int] = None) -> str:
        """The job's combined stdout/stderr; the last ``tail`` lines when given. Empty if no log yet."""
        q = shlex.quote(self.log_path)
        command = f"tail -n {int(tail)} {q} 2>/dev/null || true" if tail is not None else f"cat {q} 2>/dev/null || true"
        try:
            return self._client.exec(self.pod, command=command, timeout=30).get("stdout", "")
        except Exception:  # a log read must never mask the error that led here
            return ""

    def kill(self, signal: str = "TERM") -> bool:
        """Send ``signal`` to the job's whole process group. Returns whether anything received it.

        Nothing is signalled when the PID no longer belongs to the job (the pod
        restarted and another process took the number) or when the job has no
        ``.id`` file to check it against; the call returns ``False``.
        """
        sig = signal.upper().removeprefix("SIG")
        if not re.fullmatch(r"[A-Z0-9]+", sig):
            raise ValueError(f"Invalid signal {signal!r}")
        command = (
            f"{identity_matches(str(self.pid), self.id_file)} && "
            f"{{ kill -{sig} -- -{self.pid} 2>/dev/null || kill -{sig} {self.pid} 2>/dev/null; }}"
        )
        return bool(self._client.exec(self.pod, command=command, timeout=30).get("success"))

    def _pod_label(self) -> str:
        return self.pod.name or self.pod.huid or self.pod.id
