from types import SimpleNamespace

import pytest

from lium.cli.logs.actions import StreamLogsAction
from lium.cli.utils import CliFailure, EXIT_API_ERROR


def test_an_error_frame_fails_the_command_with_the_server_reason():
    frames = ['data: {"log": "hello"}', b'data: {"error": "Node is not reachable right now, try again shortly"}']
    ctx = {"lium": SimpleNamespace(logs=lambda *a, **k: iter(frames)), "pod": SimpleNamespace(id="p"), "tail": 10,
           "follow": False}
    lines = StreamLogsAction().execute(ctx)
    assert next(lines) == "hello"
    with pytest.raises(CliFailure) as failed:
        next(lines)
    assert failed.value.exit_code == EXIT_API_ERROR
    assert failed.value.message == "Node is not reachable right now, try again shortly"
