"""Higher-level decorators built on top of the Lium SDK."""

import ast
import atexit
import base64
import builtins
import hashlib
import inspect
import math
import os
import pickle
import re
import shlex
import sys
import tempfile
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import result_codec
from .client import Lium
from .exceptions import LiumError, RemoteExecutionError
from .models import ExecutorInfo
from .result_codec import ResultEncodingError
from .utils import gpu_short_matches

# How long the pod may outlive the call before the server removes it on its own.
# It covers the boot wait, the result download and a caller that dies mid-call.
_TTL_MARGIN = timedelta(minutes=15)
# TTL when the call itself has no timeout: still a bound, not "forever".
_TTL_NO_TIMEOUT = timedelta(hours=24)
_BOOT_TIMEOUT = 300
# A warm pod is removed server-side this long after its `keep_warm` window, should the
# caller never come back to remove it.
_WARM_MARGIN = timedelta(minutes=2)

# Pods kept alive between calls, by `_warm_key`. Shared by every decorated function in
# the process so two functions with the same machine spec share one pod.
_WARM: Dict[str, "_Warm"] = {}

_SPEC_RE = re.compile(r"^\s*(?:(\d+)\s*[x×]\s*)?(.+?)\s*$", re.IGNORECASE)


def _parse_machine(spec: str) -> Tuple[int, str]:
    """``"1xH200"`` → ``(1, "H200")``; ``"RTX 4090"`` → ``(1, "RTX4090")``; ``"2xA100"`` → ``(2, "A100")``.

    The count defaults to 1: a function offloaded to "an H200" wants one H200, not the
    first eight-GPU node the API happens to list.
    """
    m = _SPEC_RE.match(spec or "")
    count = int(m.group(1) or 1) if m else 0
    if not m or not m.group(2) or count == 0:
        raise ValueError(f"Invalid machine spec {spec!r}; use e.g. '1xH200', 'RTX4090', '2xA100'")
    return count, m.group(2).replace(" ", "").upper()


def _rentable(executor: ExecutorInfo) -> int:
    """The GPUs a rent that names no count gets: the node's free GPUs (``available_gpu_count``),
    the whole host when the API did not say. On a partially rented split host this is fewer than
    ``gpu_count`` — the same rule as ``lium up`` (``rented_gpu_count``)."""
    return executor.available_gpu_count if executor.available_gpu_count is not None else executor.gpu_count


def _rent_price(executor: ExecutorInfo) -> float:
    """What the pod bills per hour: ``price_per_gpu`` × the GPUs rented; the host price when the
    API gave no per-GPU price."""
    if executor.price_per_gpu:
        return executor.price_per_gpu * _rentable(executor)
    return executor.price_per_hour


def _select_executor(executors: List[ExecutorInfo], spec: str) -> ExecutorInfo:
    """Cheapest node that rents exactly ``count`` GPUs of the requested type.

    The count is the node's rentable GPUs (:func:`_rentable`): ``Lium.up()`` names no count, so
    the pod gets the node's free GPUs and is billed for those — a split host with 2 of 8 free is a
    ``2xA100`` here, never an ``8xA100``. The price compared and shown is that rental's
    (:func:`_rent_price`), not the whole host's.

    The type is matched the way ``lium ls --gpu`` matches it (:func:`gpu_short_matches` on the
    node's extracted ``gpu_type``): whole, never as a substring of the machine name — ``"A100"``
    names an A100 and not an RTX A1000, ``"H100"`` takes an ``H100 NVL`` and an ``H100 80GB``
    alike, a bare ``"4090"`` names the RTX 4090. A bare number that names several types
    (``"100"``: A100 and H100, as ``lium ls --gpu 100`` lists both) picks the cheapest across them.

    When nothing matches, the error names what the listing has: the same type at other counts
    (``Available: 1xA100 $1.20/h, 8xA100 $3.60/h``), else the GPU types on the listing.
    """
    count, gpu = _parse_machine(spec)
    offered = [e for e in executors if _rentable(e) > 0]   # a fully rented split host has nothing to rent
    matches = [e for e in offered if _rentable(e) == count and gpu_short_matches(gpu, e.gpu_type)]
    if not matches:
        same_type = sorted(
            {f"{_rentable(e)}x{e.gpu_type} ${_rent_price(e):.2f}/h"
             for e in offered if gpu_short_matches(gpu, e.gpu_type)}
        )
        if same_type:
            hint = f" Available: {', '.join(same_type)}."
        else:
            types = sorted({e.gpu_type for e in executors if e.gpu_type})
            hint = f" GPU types on the listing: {', '.join(types)}." if types else ""
        raise LiumError(f"No node found matching machine type: {spec}.{hint}")
    return min(matches, key=_rent_price)


def _rent_node(sdk: Lium, spec: str, pod_name: str, template_id: Optional[str]):
    """The cheapest node matching ``spec``, rented: ``(executor, $/h billed, pod dict)``.

    A backend that advertises ``rent_by_spec`` picks and rents in one call (and falls through
    to the next candidate if the pick is taken meanwhile); an older one gets today's path —
    list the fleet here, choose, rent by id — which costs three fleet listings per cold call.
    """
    count, gpu = _parse_machine(spec)
    # `Lium.rent`/`Lium.supports` land with #184 (DAH-3047); an SDK without them keeps the listing path
    if hasattr(sdk, "rent") and sdk.supports("rent_by_spec"):
        rented = sdk.rent(gpu_type=gpu, gpu_count=count, name=pod_name, template_id=template_id)
        return rented.executor, rented.price_per_hour, rented.pod
    executor = _select_executor(sdk.ls(), spec)
    pod_dict = sdk.up(executor_id=executor.id, name=pod_name, template_id=template_id)
    return executor, _rent_price(executor), pod_dict


def _say(quiet: bool, func_name: str, msg: str) -> None:
    if not quiet:
        print(f"[lium] {func_name}: {msg}", file=sys.stderr, flush=True)


# --- what travels to the pod -------------------------------------------------------------------

def _function_source(func) -> str:
    """The function's ``def`` alone: decorators and annotations stripped, dedented.

    Decorators would re-run ``@lium.machine`` on the pod; annotations are evaluated
    at definition time and usually name things (``np.ndarray``) the pod does not have.
    """
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError) as exc:
        raise LiumError(f"Cannot read the source of {func.__name__}: {exc}") from exc
    node = _def_node(func, source)
    node.decorator_list = []
    node.returns = None
    for arg in ast.walk(node.args):
        if isinstance(arg, ast.arg):
            arg.annotation = None
    return ast.unparse(node)


def _def_node(func, source: str):
    tree = ast.parse(source)
    node = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if node is None:
        raise LiumError(f"{func.__name__} must be defined with `def` (lambdas cannot be sent to a pod)")
    return node


def _code_names(code) -> Set[str]:
    """Names a code object (and its nested functions) looks up outside its locals."""
    names = set(code.co_names) - set(code.co_varnames) - set(code.co_cellvars)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            names |= _code_names(const)
    return names


def _name_loads(node) -> Set[str]:
    """Identifiers the def reads as plain names (``ast.Name`` in Load context), nested functions included.

    ``co_names`` also lists attribute names, so ``x.data`` would count as a use of a module global ``data``
    and the function would be refused for nothing; an attribute is not an ``ast.Name``.
    """
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)}


def _imported_modules(node) -> Set[str]:
    """Top-level module names the function imports itself (those exist on the pod)."""
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Import):
            found |= {alias.name.split(".")[0] for alias in sub.names}
        elif isinstance(sub, ast.ImportFrom) and sub.module:
            found.add(sub.module.split(".")[0])
    return found


def _check_portable(func) -> None:
    """Refuse, before renting anything, a function the pod could only fail on.

    Only the function's own source travels: a closure variable or a module-level
    name (constant, import, helper) is a ``NameError`` on the pod, sixty seconds and
    a few cents later.
    """
    code = func.__code__
    free = set(code.co_freevars) - {func.__name__}  # a nested function recursing into itself is fine
    if free:
        raise LiumError(
            f"{func.__name__} closes over {sorted(free)}; only the function's own source "
            "runs on the pod, so pass them as arguments instead"
        )
    try:
        node = _def_node(func, textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError):
        return  # _function_source reports unreadable source
    module_names = set(func.__globals__) - {"__builtins__", func.__name__} - _imported_modules(node)
    # A default value (`k=SCALE`, `s=os.sep`) is evaluated by the enclosing module at `def` time, so its
    # names are not in the body's co_names — yet the pod re-executes the `def` and fails on that line,
    # before any import inside the body has run (so an in-body import does not excuse a default).
    defaults = [d for d in node.args.defaults + node.args.kw_defaults if d is not None]
    default_names: Set[str] = set()
    for d in defaults:   # plain-name reads, minus what the expression binds itself (a lambda's parameter, a comprehension variable)
        bound = {a.arg for a in ast.walk(d) if isinstance(a, ast.arg)}
        bound |= {n.id for n in ast.walk(d) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        default_names |= _name_loads(d) - bound
    # a default's name that is not a builtin — a module global or a local of the enclosing function — is not on the pod
    used = ((_code_names(code) & module_names) | (default_names - set(dir(builtins)))) & _name_loads(node)
    if used:
        raise LiumError(
            f"{func.__name__} uses module-level names {sorted(used)}, which do not exist on the pod: "
            "import inside the function or pass them as arguments"
        )


def _codec_source() -> str:
    """``lium/sdk/result_codec.py`` as text: the pod runs the same encoder the caller decodes with."""
    try:
        return inspect.getsource(result_codec)
    except (OSError, TypeError) as exc:
        raise LiumError(f"Cannot read lium.sdk.result_codec's source to ship it to the pod: {exc}") from exc


def _runner_script(source: str, func_name: str, is_async: bool, args, kwargs, result_path: str) -> str:
    """The script the pod runs. Arguments travel as a pickle — the caller's own bytes, loaded on the
    caller's own pod. The result travels back as a JSON envelope plus an ``.npz`` sidecar for numpy
    arrays (``result_codec``); nothing the pod writes is unpickled here."""
    try:
        blob = base64.b64encode(pickle.dumps((args, kwargs), protocol=4)).decode()
    except Exception as exc:  # noqa: BLE001 — pickle raises many types
        raise LiumError(f"Arguments of {func_name} cannot be pickled for the pod: {exc}") from exc
    call = f"{func_name}(*_lium_args, **_lium_kwargs)"
    if is_async:
        call = f"_lium_asyncio.run({call})"
    # The user's `def` shares the runner's module namespace, so everything of ours is prefixed `_lium_`, the
    # codec lives in its own module object, and the builtins the runner uses after the user's code (`type`,
    # `str`, `bool`, `open`, `BaseException`) are read from the `builtins` module: a function named `encode`,
    # `args`, `sys`, `open` or `bool` must not break the runner.
    return f'''#!/usr/bin/env python3
import asyncio as _lium_asyncio, base64 as _lium_base64, builtins as _lium_builtins, pickle as _lium_pickle
import sys as _lium_sys, traceback as _lium_traceback, types as _lium_types

_lium_codec = _lium_types.ModuleType("lium_result_codec")   # lium/sdk/result_codec.py, shipped as text
exec(compile({_codec_source()!r}, "lium/sdk/result_codec.py", "exec"), _lium_codec.__dict__)

{source}

_lium_payload = {{'ok': False, 'type': 'RuntimeError', 'module': 'builtins', 'message': 'runner did not finish',
                 'traceback': '', 'args': None}}
_lium_arrays = {{}}
try:
    _lium_args, _lium_kwargs = _lium_pickle.loads(_lium_base64.b64decode({blob!r}))
    _lium_result = {call}
    _lium_payload = {{'ok': True, 'result': _lium_codec.encode(_lium_result, _lium_arrays, 'the result of {func_name}')}}
except _lium_builtins.BaseException as _lium_e:
    # type/message/traceback always arrive; the args only when they are plain data (encode_exception_args)
    _lium_arrays = {{}}
    _lium_payload = {{'ok': False, 'type': _lium_builtins.type(_lium_e).__name__,
                     'module': _lium_builtins.type(_lium_e).__module__,
                     'message': _lium_builtins.str(_lium_e), 'traceback': _lium_traceback.format_exc(),
                     'args': _lium_codec.encode_exception_args(_lium_e.args)}}
finally:
    _lium_payload['npz'] = _lium_builtins.bool(_lium_arrays)
    if _lium_arrays:
        _lium_codec.save_arrays({result_path + ".npz"!r}, _lium_arrays)
    with _lium_builtins.open({result_path!r}, 'w', encoding='utf-8') as _lium_f:
        _lium_f.write(_lium_codec.dumps(_lium_payload))
    if not _lium_payload['ok']:
        _lium_sys.exit(1)
'''


def _read_envelope(path: str) -> Dict[str, Any]:
    """The JSON envelope the pod wrote; anything else (a pickle, a truncated file) is a ``ValueError``."""
    with open(path, "rb") as f:
        return result_codec.loads(f.read())


def _decode_payload(envelope: Dict[str, Any], npz_path: str) -> Dict[str, Any]:
    """The envelope with its values decoded; arrays come from ``npz_path`` via ``np.load(allow_pickle=False)``."""
    arrays = result_codec.load_arrays(npz_path) if envelope.get("npz") else {}
    payload = dict(envelope)
    if payload.get("ok"):
        payload["result"] = result_codec.decode(payload.get("result"), arrays)
    elif payload.get("args") is not None:
        payload["args"] = result_codec.decode(payload["args"], {})
    return payload


def _load_result(path: str) -> Dict[str, Any]:
    """The pod's result payload from ``path`` (and ``path + '.npz'`` when it holds arrays)."""
    return _decode_payload(_read_envelope(path), path + ".npz")


def _builtin_exception(payload: Dict[str, Any]) -> Optional[Exception]:
    """The remote exception rebuilt here — when its class is a builtin ``Exception`` subclass (never
    ``SystemExit``/``KeyboardInterrupt``) that can be constructed from the shipped args, or from the
    message when the args were not plain data. Anything else stays a :class:`RemoteExecutionError`."""
    if payload.get("module") != "builtins":
        return None
    cls = getattr(builtins, payload.get("type", ""), None)
    if not (isinstance(cls, type) and issubclass(cls, Exception)):
        return None
    candidates = [tuple(payload["args"])] if payload.get("args") is not None else []
    candidates.append((payload.get("message", ""),))
    for args in candidates:
        try:
            return cls(*args)
        except Exception:  # noqa: BLE001 — a builtin with a fixed signature (UnicodeDecodeError) and other args
            continue
    return None


def _venv_path(reqs: Sequence[str]) -> str:
    """One environment per distinct requirements list, shared by every call on the pod."""
    digest = hashlib.sha1(" ".join(sorted(reqs)).encode()).hexdigest()[:10]
    return f"/tmp/lium-venv-{digest}"


def _setup_command(venv_path: str, reqs: Sequence[str]) -> str:
    """Create the venv and install ``reqs`` once; later calls find the marker and skip both.

    ``--system-site-packages`` keeps the image's own packages (the PyTorch templates ship
    torch + CUDA) visible, so ``requirements=["torch", ...]`` is satisfied in seconds
    instead of downloading torch again.
    """
    q = shlex.quote
    marker = q(f"{venv_path}/.lium-ready")
    steps = [f"python3 -m venv --system-site-packages {q(venv_path)}"]
    if reqs:
        steps.append(f"{q(venv_path)}/bin/python -m pip install -q --disable-pip-version-check {' '.join(q(r) for r in reqs)}")
    steps.append(f"touch {marker}")
    return f"if test -f {marker}; then echo LIUM_ENV_CACHED; else {' && '.join(steps)}; fi"


def _run_streaming(sdk: Lium, pod, command: str) -> Dict[str, Any]:
    """Run ``command`` on the pod, relaying its stdout/stderr to ours as it happens.

    Returns the same dict as :meth:`Lium.exec` so the caller can keep the captured output.
    """
    chunks: Dict[str, List[str]] = {"stdout": [], "stderr": []}
    streams = {"stdout": sys.stdout, "stderr": sys.stderr}
    gen = sdk.stream_exec(pod, command=command, pty=False)
    while True:
        try:
            chunk = next(gen)
        except StopIteration as stop:
            exit_code = stop.value
            break
        chunks[chunk["type"]].append(chunk["data"])
        streams[chunk["type"]].write(chunk["data"])
        streams[chunk["type"]].flush()
    return {
        "stdout": "".join(chunks["stdout"]),
        "stderr": "".join(chunks["stderr"]),
        "exit_code": exit_code,
        "success": exit_code == 0,
    }


def _raise_remote(payload: Optional[Dict[str, Any]], func_name: str, exec_result: Dict[str, Any], timeout) -> None:
    """Turn what came back from the pod into the caller's exception."""
    exit_code = exec_result.get("exit_code")
    common = dict(exit_code=exit_code, stdout=exec_result.get("stdout", ""), stderr=exec_result.get("stderr", ""))
    if payload is None:
        if timeout and exit_code == 124:  # coreutils timeout
            raise RemoteExecutionError(f"{func_name} exceeded timeout={timeout}s and was killed", **common)
        detail = exec_result.get("stderr") or exec_result.get("stdout") or "no result file and no output"
        if exit_code == -1:  # the ssh session got an exit-signal instead of an exit status
            detail = f"the process was killed by a signal (out of memory?)\n{detail}"
        raise RemoteExecutionError(f"{func_name} produced no result:\n{detail}", **common)
    cause = RemoteExecutionError(
        f"{payload['type']}: {payload['message']}\n\nRemote traceback:\n{payload.get('traceback', '')}",
        exception_type=payload["type"], remote_traceback=payload.get("traceback", ""), **common,
    )
    if payload["type"] == "ResultEncodingError":  # raised by the codec on the pod: the result does not travel
        raise ResultEncodingError(payload["message"]) from cause
    exc = _builtin_exception(payload)
    if exc is not None:
        raise exc from cause
    raise cause


class _Warm:
    """A pod held for the next call. ``owned`` says this process rented it; a pod found by name
    from an earlier run is not ours to remove at exit, and its removal window is left alone
    unless a call asks for warmth again."""

    def __init__(self, sdk: Lium, pod, executor: ExecutorInfo, keep_warm: float, quiet: bool = False,
                 owned: bool = True, previous_removal: Optional[str] = None, *, hourly: float):
        self.sdk, self.pod, self.executor, self.keep_warm, self.quiet = sdk, pod, executor, keep_warm, quiet
        self.owned = owned
        # what the pod bills per hour: the `renting …` figure for a pod this process rented, the pod row's
        # own price (``ps()`` anchors ``executor.price_per_hour`` on it) for one found by name
        self.hourly = hourly
        self.previous_removal = previous_removal   # the found pod's removal time before this call re-armed it


def _warm_key(spec: str, template_id: Optional[str]) -> str:
    count, gpu = _parse_machine(spec)
    return hashlib.sha1(f"{count}x{gpu}|{template_id or ''}".encode()).hexdigest()[:8]


def _schedule_removal(sdk: Lium, pod, delay: timedelta, say) -> None:
    """Server-side safety net: the pod goes away even if this process does not."""
    try:
        sdk.schedule_termination(_pod_ref(pod), termination_time=(datetime.now(timezone.utc) + delay).isoformat())
    except Exception as exc:  # noqa: BLE001 — a missing TTL must not fail the call
        say(f"warning: could not schedule pod removal ({exc}); remove it yourself if this process dies")


def _future_time(iso: Optional[str]) -> Optional[str]:
    """``iso`` when it parses and lies ahead of now, else None."""
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return iso if when > datetime.now(timezone.utc) else None


def _find_warm(sdk: Lium, key: str, say):
    """A pod this process (or an earlier one, by name) left warm for this machine spec."""
    warm = _WARM.get(key)
    live = {p.id: p for p in sdk.ps() if p.status.upper() == "RUNNING" and p.ssh_cmd}
    if warm and warm.pod.id in live:
        return warm
    _WARM.pop(key, None)
    for pod in live.values():
        if pod.name == f"lium-fn-{key}" and pod.executor:
            rented = pod.gpu_count if pod.gpu_count is not None else pod.executor.gpu_count
            hourly = pod.executor.price_per_hour   # ps() anchors it on the pod row's own price: what this pod bills
            say(f"reusing warm pod {pod.huid} ({rented}x{pod.executor.gpu_type} ${hourly:.2f}/h)")
            return _Warm(sdk, pod, pod.executor, 0, owned=False, previous_removal=getattr(pod, "removal_scheduled_at", None),
                         hourly=hourly)
    return None


def _close_all() -> None:
    """atexit: remove pods held for `.map()`; leave `keep_warm` pods, and pods another run rented, to their TTL."""
    for key, warm in list(_WARM.items()):
        _WARM.pop(key, None)
        if warm.keep_warm:
            _say(warm.quiet, "lium.machine", f"pod {warm.pod.huid} stays warm {warm.keep_warm:.0f}s for the next run, then is removed")
            continue
        if not warm.owned:
            _say(warm.quiet, "lium.machine", f"pod {warm.pod.huid} was found warm, not rented here; left to its own removal time")
            continue
        try:
            warm.sdk.down(_pod_ref(warm.pod))
        except Exception:  # noqa: BLE001 — best effort at interpreter shutdown; the TTL remains
            pass


atexit.register(_close_all)


def machine(
    machine: str,
    template_id: Optional[str] = None,
    cleanup: bool = True,
    requirements: Optional[Sequence[str]] = None,
    *,
    timeout: Optional[float] = 3600,
    keep_warm: float = 0,
    local: bool = False,
    quiet: bool = False,
):
    """Decorator to execute a function on a remote Lium machine.

    Creates a new pod, sends function source code and executes it remotely,
    returns the result, and optionally cleans up the pod.

    The decorated function also offers ``f.remote(*a)`` (same as ``f(*a)``),
    ``f.local(*a)`` (run the original here), ``f.map(iterable)`` (run every item on one
    pod, rented once) and ``f.close()`` (remove the pod kept by ``keep_warm``).

    Arguments travel as a pickle: your own bytes, loaded on your own pod as they are. The
    result comes back from provider hardware, so nothing it contains is unpickled here:
    it travels as a JSON envelope plus an ``.npz`` sidecar for numpy arrays, read with
    ``allow_pickle=False``. What round-trips, exactly: ``None``, ``bool``, ``int``,
    ``float``, ``str``, ``bytes``; ``list``, ``tuple``, ``set``, ``frozenset`` and
    ``dict`` of those, nested; ``datetime``/``date``/``time``/``timedelta``, ``Decimal``,
    ``pathlib.Path``, ``uuid.UUID``; ``numpy.ndarray`` (any dtype without Python objects,
    any shape) and numpy scalars. Anything else — a dataclass, an ``Enum``, an
    ``OrderedDict``, a tensor, an ndarray subclass — raises :class:`ResultEncodingError`
    on the pod, naming the type and its place in the result, and is re-raised here
    (return ``.tolist()``, ``dict(x)``, ``x.value`` instead).
    Only the function's own ``def`` is sent: import what it needs inside the body and
    pass everything else as arguments. Whatever the function prints is relayed to this
    process's stdout/stderr while it runs. An exception raised on the pod is re-raised
    here with the same type when that type is a builtin (``except ValueError`` works;
    its args come along when they are plain data, else its message); other types come
    back as :class:`RemoteExecutionError`. Either way the ``__cause__`` is a
    :class:`RemoteExecutionError` carrying the remote traceback, exit code and captured
    output.

    Args:
        machine: ``"<count>x<gpu>"`` or ``"<gpu>"`` — e.g. ``"1xH200"``, ``"RTX4090"``,
            ``"2xA100"``. The count defaults to 1. The GPU is named as ``lium ls --gpu``
            takes it (``H100``, ``RTX4090``, ``rtx pro 6000``, a bare ``4090``) and has
            to match the node's type whole — ``"A100"`` never rents an RTX A1000. The
            cheapest node with exactly that many GPUs of that type free to rent is rented
            (a split host with 2 of 8 free is a ``2xA100``) and billed for those — by the
            backend in one call (:meth:`Lium.rent`) when it offers ``rent_by_spec``, else
            picked here from a listing; when none matches, the error names the types the
            listing has.
        template_id: Docker template ID (optional, uses the node's default if not specified)
        cleanup: Whether to delete the pod after execution (default: True)
        requirements: Optional iterable of pip-installable packages to install on the pod.
            They go into a venv that also sees the image's own packages, created and
            populated once per pod and reused by later calls with the same list.
        timeout: Seconds the function may run on the pod before it is killed (default 1 h;
            ``None`` for no limit; ``0`` or a negative number is refused when decorating).
            A fraction of a second is rounded up to the next second for the kill. The pod is also scheduled for removal at
            ``timeout + 15 min`` (24 h when ``timeout=None``) so a caller that dies
            mid-call cannot leave it billing.
        keep_warm: Seconds the pod stays after a call for the next one — from this process
            or the next run of the script (the pod is found by name). Default 0: the pod
            is removed when the call returns. The pod is scheduled for removal server-side
            ``keep_warm + 2 min`` after each call, so nothing depends on the caller coming back.
        local: Run the function in this process instead (``LIUM_MACHINE_LOCAL=1`` does the
            same for every decorated function) — for tests and offline work.
        quiet: Suppress the one-line progress messages written to stderr.
    """

    if timeout is not None and timeout <= 0:
        raise ValueError(f"timeout must be a positive number of seconds or None for no limit, got {timeout!r}")

    def decorator(func):
        run_local = local or os.environ.get("LIUM_MACHINE_LOCAL") == "1"
        if not run_local:
            _check_portable(func)
            func_source = _function_source(func)
        is_async = inspect.iscoroutinefunction(func)
        key = _warm_key(machine, template_id)
        holding = [0]  # > 0 while `.map()` runs: keep the pod between items

        @wraps(func)
        def wrapper(*args, **kwargs):
            if run_local:
                return func(*args, **kwargs)
            call_id = uuid.uuid4().hex[:8]
            remote_runner = f"/tmp/lium-{call_id}.py"
            remote_result = f"/tmp/lium-{call_id}.json"   # + ".npz" beside it when the result holds arrays
            runner_script = _runner_script(func_source, func.__name__, is_async, args, kwargs, remote_result)

            # Initialize SDK
            sdk = Lium()
            pod_info = None
            keep = cleanup and (keep_warm > 0 or holding[0] > 0)
            started = time.time()
            say = lambda msg: _say(quiet, func.__name__, msg)  # noqa: E731
            ttl = (timedelta(seconds=timeout) + _TTL_MARGIN) if timeout else _TTL_NO_TIMEOUT
            if keep:
                ttl += timedelta(seconds=keep_warm)

            try:
                # A pod left warm for this machine spec (by this process or the previous run) is
                # used whatever this call's keep_warm is; it is left as warm as it was found.
                warm = _find_warm(sdk, key, say) if cleanup else None
                if warm:
                    sdk, pod_info, executor, hourly = warm.sdk, warm.pod, warm.executor, warm.hourly
                    _schedule_removal(sdk, pod_info, ttl, say)  # re-arm: this call may run up to `timeout`
                else:
                    # Steps 1-2: rent the cheapest node renting "<count>x<gpu>" (its free GPUs, not the
                    # whole host; a fixed pod name lets the next run of the script find it)
                    pod_name = f"lium-fn-{key}" if keep else f"remote-{func.__name__}-{int(time.time())}"
                    executor, hourly, pod_dict = _rent_node(sdk, machine, pod_name, template_id)
                    say(
                        f"rented {_parse_machine(machine)[0]}x{executor.gpu_type} ${hourly:.2f}/h "
                        f"({executor.huid}, {(executor.location or {}).get('country', '?')}), "
                        f"removal in {ttl.total_seconds() / 3600:.1f}h"
                    )
                    pod_info = pod_dict  # enough for cleanup (dict with id) until wait_ready returns
                    _schedule_removal(sdk, pod_dict, ttl, say)

                    # Wait for pod to be ready
                    pod_info = sdk.wait_ready(pod_dict, timeout=_BOOT_TIMEOUT)
                    if not pod_info:
                        pod_info = _pod_ref(pod_dict)
                        raise LiumError(f"Pod {pod_name} failed to start within {_BOOT_TIMEOUT}s")
                    say(f"pod ready in {time.time() - started:.0f}s")

                # Step 3: Upload the runner script (function source + pickled arguments)
                with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                    runner_file = f.name
                    f.write(runner_script)

                # One SSH connection for the upload, the environment, the run and the download
                with sdk.ssh_session(pod_info):
                    try:
                        sdk.upload(pod_info, local=runner_file, remote=remote_runner)

                        # Steps 4-5: one round trip creates the environment and installs the
                        # requirements — or finds both already there from an earlier call.
                        reqs = [req for req in (requirements or []) if req]
                        venv_path = _venv_path(reqs)
                        venv_python = f"{venv_path}/bin/python"
                        if reqs:
                            say(f"preparing environment ({len(reqs)} package(s): {', '.join(reqs)})")
                        t_env = time.time()
                        env_result = sdk.exec(pod_info, command=_setup_command(venv_path, reqs))
                        if not env_result['success']:
                            raise LiumError(
                                f"Failed preparing the environment ({', '.join(reqs) or 'no requirements'}):\n"
                                f"{env_result['stderr'] or env_result['stdout']}"
                            )
                        if "LIUM_ENV_CACHED" in env_result['stdout']:
                            say("environment already on the pod")
                        elif reqs:
                            say(f"environment ready in {time.time() - t_env:.0f}s")

                        # Step 6: Execute runner via virtual environment python, bounded by `timeout`,
                        # relaying its output live (-u: no block buffering behind the ssh channel).
                        # The TTL armed at rent time has been running since `up()`; boot, upload and
                        # pip install may have eaten most of its 15 min margin, so it is armed again
                        # here and the run gets its full window.
                        _schedule_removal(sdk, pod_info, ttl, say)  # setup is done: give the run its full window
                        say("running")
                        run_cmd = f"{shlex.quote(venv_python)} -u {remote_runner}"
                        if timeout:
                            # TERM first, KILL 5 s later. (`-s KILL` would kill the process group,
                            # `timeout` included, and the ssh session would report no exit status.)
                            # ceil, not int(): int(0.5) is 0, which coreutils reads as "no limit"
                            run_cmd = f"timeout -k 5 {math.ceil(timeout)} {run_cmd}"
                        exec_result = _run_streaming(sdk, pod_info, run_cmd)

                        # Step 7: Download the result (also when the run failed: it carries the exception)
                        payload = None
                        with tempfile.NamedTemporaryFile(delete=False) as f:
                            result_file = f.name
                        npz_file = result_file + ".npz"
                        try:
                            sdk.download(pod_info, remote=remote_result, local=result_file)
                        except (OSError, IOError):
                            payload = None  # the runner never got to write it
                        else:
                            try:
                                # the pod wrote these bytes: a JSON envelope, and arrays in an .npz read
                                # without pickle (result_codec) — nothing from the pod is unpickled here
                                envelope = _read_envelope(result_file)
                                if envelope.get("npz"):
                                    sdk.download(pod_info, remote=remote_result + ".npz", local=npz_file)
                                payload = _decode_payload(envelope, npz_file)
                            except Exception as exc:  # noqa: BLE001 — not an envelope, a pickled .npz, numpy missing locally
                                raise RemoteExecutionError(
                                    f"the result of {func.__name__} could not be loaded locally: {exc}. "
                                    "Return plain Python types (str(), .tolist(), .cpu().numpy()) or install "
                                    "the missing package here",
                                    exit_code=exec_result.get("exit_code"),
                                ) from exc
                        finally:
                            for path in (result_file, npz_file):
                                if os.path.exists(path):
                                    os.unlink(path)

                        if payload and payload.get('ok'):
                            elapsed = time.time() - started
                            say(f"done in {elapsed:.0f}s (~${hourly * elapsed / 3600:.4f})")
                            return payload['result']
                        _raise_remote(payload, func.__name__, exec_result, timeout)

                    finally:
                        # Clean up local temp file
                        os.unlink(runner_file)
                        # Remove this call's files when the pod stays alive (the venv stays: it is the cache)
                        if keep or warm or not cleanup:
                            try:
                                sdk.exec(pod_info, command=f"rm -f {remote_runner} {remote_result} {remote_result}.npz")
                            except Exception as exc:  # noqa: BLE001 — best-effort cleanup; the call's result is already in hand
                                say(f"could not remove this call's files on the pod ({exc}); they are under /tmp, the venv cache stays")

            finally:

                # Step 8: Release the pod — remove it, or keep it warm for the next call
                if pod_info and (keep or warm) and getattr(pod_info, "ssh_cmd", None):
                    owned = warm.owned if warm else True   # a pod this call rented is ours
                    stay = max(keep_warm, warm.keep_warm if warm else 0)
                    _WARM[key] = _Warm(sdk, pod_info, executor, stay, quiet, owned=owned,
                                       previous_removal=warm.previous_removal if warm else None, hourly=hourly)
                    if stay and (owned or keep_warm):
                        # ours: re-arm the window; found by name: only when this call asked for warmth
                        _schedule_removal(sdk, pod_info, timedelta(seconds=stay) + _WARM_MARGIN, say)
                        say(f"pod stays warm {stay:.0f}s")
                    elif not owned:
                        # found by name and this call asked for no warmth: put back the removal time the run
                        # that rented it had set (the start of the call moved it out to cover the run);
                        # when that time has passed, the start-of-call TTL stands, so the pod still goes
                        previous = _future_time(warm.previous_removal) if warm else None
                        if previous:
                            try:
                                sdk.schedule_termination(_pod_ref(pod_info), termination_time=previous)
                                say(f"pod left as warm as it was found (removal at {previous})")
                            except Exception as exc:  # noqa: BLE001 — the start-of-call TTL remains
                                say(f"warning: could not restore the pod's removal time ({exc}); it keeps this call's TTL")
                        else:
                            say("pod left as warm as it was found; its previous removal time has passed, this call's TTL stands")
                elif cleanup and pod_info:
                    try:
                        sdk.down(_pod_ref(pod_info))
                        say("pod removed")
                    except Exception:
                        say("warning: could not remove the pod; it is scheduled for removal server-side")

        def close():
            """Remove the pod kept warm for this machine spec (no-op when there is none).

            An explicit close removes a found pod too: the pod the previous run of this script
            left warm is the user's, and asking for it to go is their call."""
            warm = _WARM.pop(key, None)
            if warm:
                warm.sdk.down(_pod_ref(warm.pod))
                _say(quiet, func.__name__, "warm pod removed")

        def map(items):  # noqa: A001 — mirrors the builtin on purpose
            """Run the function on every item of ``items``, on one pod rented once."""
            holding[0] += 1
            try:
                return [wrapper(item) for item in items]
            finally:
                holding[0] -= 1
                if holding[0] == 0 and not keep_warm and cleanup:
                    warm = _WARM.get(key)
                    if warm and not warm.owned:
                        # found by name from an earlier run: not ours to remove. The last item's call
                        # already put its removal time back (Step 8); just stop holding it.
                        _WARM.pop(key, None)
                        _say(quiet, func.__name__, f"pod {warm.pod.huid} was found warm, not rented here; left as found")
                    else:
                        close()

        wrapper.remote = wrapper
        wrapper.local = func
        wrapper.map = map
        wrapper.close = close
        return wrapper

    return decorator


class _PodRef:
    """The one attribute ``down``/``schedule_termination`` need before ``wait_ready`` returns a PodInfo."""

    def __init__(self, pod_id: str):
        self.id = pod_id


def _pod_ref(pod):
    if isinstance(pod, dict):
        return _PodRef(pod["id"])
    return pod


__all__ = ["machine"]
