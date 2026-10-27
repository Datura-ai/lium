"""The pod writes the result file on provider hardware: `_load_result` must refuse pickle and malformed envelopes.

Kept from test_sdk_machine.py when the suite was cut (DAH-4027 review)."""

import pytest

from lium.sdk import decorators as D


def test_a_result_file_holding_pickle_bytes_is_refused_by_the_loader(tmp_path):
    """The result file is written by the pod (provider hardware). A pickle in place of the JSON envelope,
    or an .npz whose array needs pickle, must be refused — never loaded — whatever it names."""
    import os
    import pickle

    class Gadget:
        def __reduce__(self):
            return (os.system, ("echo pwned > /dev/null",))

    path = tmp_path / "result.json"
    path.write_bytes(pickle.dumps({"ok": True, "result": Gadget()}, protocol=4))
    with pytest.raises(ValueError, match="not a JSON envelope"):
        D._load_result(str(path))

    np = pytest.importorskip("numpy")
    path.write_text('{"ok": true, "npz": true, "result": {"__lium__": "ndarray", "key": "a0"}}')
    with open(str(path) + ".npz", "wb") as f:
        np.savez(f, a0=np.array([Gadget()], dtype=object))          # numpy pickles object arrays on write
    with pytest.raises(ValueError, match="allow_pickle=False"):      # and refuses them on read without pickle
        D._load_result(str(path))

    path.write_text('{"ok": true, "npz": true, "result": {"__lium__": "ndarray", "key": "missing"}}')
    with open(str(path) + ".npz", "wb") as f:
        np.savez(f, a0=np.arange(2))
    with pytest.raises(ValueError, match="names array 'missing'"):
        D._load_result(str(path))


def test_a_malformed_envelope_is_refused_not_a_key_error(tmp_path):
    path = tmp_path / "result.json"
    for text, why in [
        ('{"ok": false}', "'type' is not a string"),
        ('{"ok": "yes", "result": 1}', "no boolean 'ok'"),
        ('{"ok": true}', "'ok' without a 'result'"),
        ('{"ok": false, "type": ["x"], "message": "m"}', "'type' is not a string"),
        ('{"ok": false, "type": "ValueError", "message": "m", "args": 3}', "'args' is not a list"),
        ('{"ok": true, "result": 1, "npz": "yes"}', "'npz' is not a boolean"),
        ('[1, 2]', "no boolean 'ok'"),
    ]:
        path.write_text(text)
        with pytest.raises(ValueError, match=why):
            D._load_result(str(path))
