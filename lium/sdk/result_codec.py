"""How a ``@lium.machine`` result travels back from the pod.

The pod runs on provider hardware, so whoever controls the node controls the result file, and nothing in
that file may run code on the caller's machine. The result is therefore a JSON envelope — plain Python
types as JSON values, a few stdlib value types as tagged strings — and numpy arrays go into an ``.npz``
sidecar that the caller reads with ``allow_pickle=False``. Anything else raises :class:`ResultEncodingError`
on the pod, naming the type and where it sits in the result, before the file is written.

Both ends run this module: ``decorators._runner_script`` ships its source to the pod, where :func:`encode`
runs, and the caller decodes with :func:`decode` from the same file. It may import only the standard
library and, when present, numpy — the pod has nothing else.

What round-trips (``type(x) is`` exactly, no subclasses — an ``IntEnum`` or an ``OrderedDict`` is refused
with a hint rather than coming back as a different type):

* ``None``, ``bool``, ``int``, ``float``, ``str``, ``bytes``;
* ``list``, ``tuple``, ``set``, ``frozenset`` and ``dict`` of the above (any of them as keys), nested;
* ``datetime.datetime`` (an aware value comes back with a fixed UTC offset), ``date``, ``time``,
  ``timedelta``, ``decimal.Decimal``, ``pathlib.Path`` (``PosixPath`` or ``PurePosixPath`` on the pod;
  comes back as ``Path``), ``uuid.UUID``;
* ``numpy.ndarray`` of any dtype without Python objects — numeric, bool, string, datetime64/timedelta64,
  structured; any shape including 0-d — and numpy's own scalar types (``np.float64(1.5)`` comes back as
  ``np.float64``; a subclass of one is refused).

What the caller constructs from a file the pod wrote, and nothing else: the JSON scalars and containers,
the tagged values above through their own parsers (``fromisoformat``, ``Decimal(str)``, ``Path(str)``,
``UUID(str)``, ``b64decode``), and arrays through ``numpy.load(allow_pickle=False)``. No name in the file is
resolved to a class or a callable.
"""

import base64
import datetime
import decimal
import json
import pathlib
import uuid
from typing import Any, Dict, List, Optional

try:
    import numpy as _np
except ImportError:  # the pod's image or the caller's machine may not have numpy; only array results need it
    _np = None

TAG = "__lium__"
SUPPORTED = (
    "None, bool, int, float, str, bytes, list, tuple, set, frozenset, dict, datetime/date/time/timedelta, "
    "decimal.Decimal, pathlib.Path, uuid.UUID, numpy.ndarray and numpy scalars"
)
_PLAIN = (type(None), bool, int, float, str)
_PATHS = (pathlib.PurePosixPath, pathlib.PosixPath)   # the pod is Linux; a Windows path object has no meaning there


class ResultEncodingError(TypeError):
    """The function's result holds a value that does not travel back from the pod.

    Raised on the pod before the result file is written; the message names the type and its place in
    the result. The caller re-raises it with the same message.
    """


def encode(obj: Any, arrays: Dict[str, Any], what: str = "the result") -> Any:
    """``obj`` as JSON-ready data; numpy arrays are moved into ``arrays`` (name → array) for the sidecar."""
    return _encode(obj, arrays, what, "")


def encode_exception_args(args: tuple) -> Optional[List[Any]]:
    """An exception's ``args`` when every one is plain data, else ``None`` (the caller then uses the message)."""
    scratch: Dict[str, Any] = {}
    try:
        encoded = [_encode(a, scratch, "the exception's args", f"[{i}]") for i, a in enumerate(args)]
    except Exception:  # noqa: BLE001 — a refused type, a self-referencing arg (RecursionError): the message still travels
        return None
    return None if scratch else encoded  # an array inside an exception is not worth a sidecar


def decode(value: Any, arrays: Dict[str, Any]) -> Any:
    """The Python value an envelope stands for; ``arrays`` is the loaded ``.npz`` (name → array)."""
    if isinstance(value, list):
        return [decode(v, arrays) for v in value]
    if not isinstance(value, dict):
        return value  # None, bool, int, float, str
    tag = value.get(TAG)
    if tag is None:
        return {k: decode(v, arrays) for k, v in value.items()}
    if tag in ("ndarray", "npscalar"):
        key = value.get("key")
        if not isinstance(key, str) or key not in arrays:
            raise ValueError(f"the result envelope names array {key!r}, which the .npz sidecar does not hold")
        if tag == "ndarray":
            return arrays[key]
        if arrays[key].ndim != 0:
            raise ValueError(f"the result envelope tags array {key!r} as a scalar but it has shape {arrays[key].shape}")
        return arrays[key][()]
    if tag == "bytes":
        return base64.b64decode(value["b64"])
    if tag in ("tuple", "set", "frozenset"):
        return {"tuple": tuple, "set": set, "frozenset": frozenset}[tag](decode(v, arrays) for v in value["items"])
    if tag == "dict":
        return {decode(k, arrays): decode(v, arrays) for k, v in value["items"]}
    if tag == "datetime":
        return datetime.datetime.fromisoformat(value["value"])
    if tag == "date":
        return datetime.date.fromisoformat(value["value"])
    if tag == "time":
        return datetime.time.fromisoformat(value["value"])
    if tag == "timedelta":
        return datetime.timedelta(days=value["days"], seconds=value["seconds"], microseconds=value["microseconds"])
    if tag == "Decimal":
        return decimal.Decimal(value["value"])
    if tag == "Path":
        return pathlib.Path(value["value"])
    if tag == "UUID":
        return uuid.UUID(value["value"])
    raise ValueError(f"unknown tag {tag!r} in the result envelope")


def save_arrays(path: str, arrays: Dict[str, Any]) -> None:
    """Write the sidecar. Every array was checked by :func:`encode` to hold no Python objects."""
    with open(path, "wb") as f:
        _np.savez(f, **arrays)


def load_arrays(path: str) -> Dict[str, Any]:
    """Read the sidecar without pickle: a file that needs it (object arrays) is refused by numpy."""
    if _np is None:
        raise ImportError("the result holds numpy arrays; install numpy here to receive it")
    with _np.load(path, allow_pickle=False) as npz:
        arrays = {name: npz[name] for name in npz.files}
    for name, arr in arrays.items():
        if not isinstance(arr, _np.ndarray):  # a zip member without the NPY header comes back as bytes
            raise ValueError(f"the .npz member {name!r} is not an array")
    return arrays


def dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload)


def loads(raw: bytes) -> Dict[str, Any]:
    """The envelope, or ``ValueError`` for anything that is not one (a pickle, a truncated file, a wrong shape)."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"the result file is not a JSON envelope: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise ValueError("the result file is not a JSON envelope: no boolean 'ok' field")
    if not isinstance(payload.get("npz", False), bool):
        raise ValueError("the result file is not a JSON envelope: 'npz' is not a boolean")
    if payload["ok"]:
        if "result" not in payload:
            raise ValueError("the result file is not a JSON envelope: 'ok' without a 'result'")
        return payload
    for field in ("type", "message"):
        if not isinstance(payload.get(field), str):
            raise ValueError(f"the result file is not a JSON envelope: the exception's {field!r} is not a string")
    for field in ("module", "traceback"):
        if not isinstance(payload.get(field, ""), str):
            raise ValueError(f"the result file is not a JSON envelope: the exception's {field!r} is not a string")
    if payload.get("args") is not None and not isinstance(payload["args"], list):
        raise ValueError("the result file is not a JSON envelope: the exception's 'args' is not a list")
    return payload


def _encode(obj: Any, arrays: Dict[str, Any], what: str, path: str) -> Any:
    if _np is not None:
        if type(obj) is _np.ndarray:
            return _array(obj, arrays, what, path, "ndarray")
        if isinstance(obj, _np.generic):
            if type(obj).__module__ != "numpy":  # a subclass of a numpy scalar type would come back as its base
                _refuse(obj, what, path, "return x.item() or the numpy type itself (numpy.float64(x))")
            return _array(_np.asarray(obj), arrays, what, path, "npscalar")
        if isinstance(obj, _np.ndarray):  # matrix, MaskedArray, recarray, memmap: the subclass would be lost
            _refuse(obj, what, path, "return arr.view(numpy.ndarray) (a masked array: arr.filled())")
    t = type(obj)
    if t in _PLAIN:
        return obj
    if t is bytes:
        return {TAG: "bytes", "b64": base64.b64encode(obj).decode("ascii")}
    if t is list:
        return [_encode(v, arrays, what, f"{path}[{i}]") for i, v in enumerate(obj)]
    if t in (tuple, set, frozenset):
        return {TAG: t.__name__, "items": [_encode(v, arrays, what, f"{path}[{i}]") for i, v in enumerate(obj)]}
    if t is dict:
        if TAG not in obj and all(type(k) is str for k in obj):
            return {k: _encode(v, arrays, what, f"{path}[{k!r}]") for k, v in obj.items()}
        return {TAG: "dict", "items": [[_encode(k, arrays, what, f"{path} key {k!r}"),
                                        _encode(v, arrays, what, f"{path}[{k!r}]")] for k, v in obj.items()]}
    if t is datetime.datetime:
        return {TAG: "datetime", "value": obj.isoformat()}
    if t is datetime.date:
        return {TAG: "date", "value": obj.isoformat()}
    if t is datetime.time:
        return {TAG: "time", "value": obj.isoformat()}
    if t is datetime.timedelta:
        return {TAG: "timedelta", "days": obj.days, "seconds": obj.seconds, "microseconds": obj.microseconds}
    if t is decimal.Decimal:
        return {TAG: "Decimal", "value": str(obj)}
    if t in _PATHS:
        return {TAG: "Path", "value": str(obj)}
    if t is uuid.UUID:
        return {TAG: "UUID", "value": str(obj)}
    hint = "return plain data instead (dict(x), list(x), x.value, str(x), .tolist())"
    if isinstance(obj, (list, tuple, set, frozenset, dict)):
        hint = f"return {t.__mro__[1].__name__}(x) instead"
    _refuse(obj, what, path, hint)


def _array(arr: Any, arrays: Dict[str, Any], what: str, path: str, kind: str) -> Dict[str, str]:
    if arr.dtype.hasobject:
        raise ResultEncodingError(
            f"{what}{path} is a numpy array of dtype object, which does not travel back from the pod (it would "
            f"need pickle); use a numeric, string or structured dtype, or .tolist()"
        )
    key = f"a{len(arrays)}"
    arrays[key] = arr
    return {TAG: kind, "key": key}


def _refuse(obj: Any, what: str, path: str, hint: str) -> None:
    t = type(obj)
    name = t.__qualname__ if t.__module__ in ("builtins", "__main__") else f"{t.__module__}.{t.__qualname__}"
    where = f"{what}{path}" if path else what
    raise ResultEncodingError(
        f"{where} is a {name}, which does not travel back from the pod; {hint}. What does: {SUPPORTED}."
    )


__all__ = ["ResultEncodingError", "SUPPORTED", "TAG", "decode", "encode", "encode_exception_args",
           "load_arrays", "loads", "dumps", "save_arrays"]
