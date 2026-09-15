"""Opt-in crash reporting (DAH-2057).

Off by default, on only by the user's hand, and when on it sends the crash and the
command name — not the arguments, not the machine, not the account.
"""

from pathlib import PurePosixPath, PureWindowsPath

import click
import pytest
import sentry_sdk
from click.testing import CliRunner

from lium.cli import telemetry
from lium.cli.utils import EXIT_GENERAL_ERROR, handle_errors


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_TELEMETRY", raising=False)
    monkeypatch.delenv("LIUM_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("LIUM_SENTRY_DSN", raising=False)
    monkeypatch.delenv("LIUM_BASE_URL", raising=False)
    monkeypatch.setattr(telemetry, "_initialised", False)
    yield
    sentry_sdk.get_global_scope().set_client(None)


def test_off_by_default():
    assert telemetry.enabled() is False


@pytest.mark.parametrize("value, expected", [("1", True), ("true", True), ("yes", True), ("0", False), ("false", False)])
def test_environment_variable_decides(monkeypatch, value, expected):
    monkeypatch.setenv("LIUM_TELEMETRY", value)

    assert telemetry.enabled() is expected


def test_config_file_decides_when_the_variable_is_absent():
    from lium.cli.settings import ConfigManager

    ConfigManager().set("telemetry.enabled", "true")
    assert telemetry.enabled() is True

    ConfigManager().set("telemetry.enabled", "false")
    assert telemetry.enabled() is False


def test_enabled_without_a_dsn_still_sends_nothing(monkeypatch):
    monkeypatch.setenv("LIUM_TELEMETRY", "1")
    monkeypatch.setattr(telemetry, "DEFAULT_SENTRY_DSN", "")
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: pytest.fail("SDK initialised without a DSN"))

    assert telemetry.init("lium up", "0.0.33") is False
    assert telemetry.report(RuntimeError("x")) is False


def test_the_shipped_dsn_is_the_lium_cli_project_and_an_empty_override_turns_it_off(monkeypatch):
    # DAH-3121: the default is the project's public client key (it can only send, never read)
    assert telemetry.DEFAULT_SENTRY_DSN.startswith("https://") and "ingest" in telemetry.DEFAULT_SENTRY_DSN
    assert telemetry.dsn() == telemetry.DEFAULT_SENTRY_DSN

    monkeypatch.setenv("LIUM_SENTRY_DSN", "https://other@o0.ingest.sentry.io/1")
    assert telemetry.dsn() == "https://other@o0.ingest.sentry.io/1"

    monkeypatch.setenv("LIUM_SENTRY_DSN", "")
    monkeypatch.setenv("LIUM_TELEMETRY", "1")
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: pytest.fail("SDK initialised with an empty DSN"))
    assert telemetry.dsn() == ""
    assert telemetry.init("lium up", "0.0.33") is False


@pytest.mark.parametrize(
    "base_url, host, env",
    [
        (None, "lium.io", "prod"),
        ("https://lium.io/api", "lium.io", "prod"),
        ("https://api.lium.io", "api.lium.io", "prod"),
        ("https://staging.lium.io/api", "staging.lium.io", "staging"),
        ("https://api.staging.lium.io/api", "api.staging.lium.io", "staging"),
        ("http://localhost:8000", "localhost", "dev"),
        ("http://10.0.0.4:8000/api", "10.0.0.4", "dev"),
        ("not a url", "lium.io", "prod"),
    ],
)
def test_environment_follows_the_api_host(monkeypatch, base_url, host, env):
    if base_url is None:
        monkeypatch.delenv("LIUM_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("LIUM_BASE_URL", base_url)

    assert telemetry.api_host() == host
    assert telemetry.environment() == env


@pytest.mark.parametrize("bad_dsn", ["garbage", "https://x", "https://public@/0", "https://k@h/[/x]1"])
def test_a_malformed_dsn_override_warns_on_stderr_and_reporting_stays_off(monkeypatch, capsys, bad_dsn):
    # the last one: the SDK quotes the DSN in its message, and `[/x]` is Rich markup — unescaped, the warning itself raised
    monkeypatch.setenv("LIUM_TELEMETRY", "1")
    monkeypatch.setenv("LIUM_SENTRY_DSN", bad_dsn)

    assert telemetry.init("lium ls", "0.0.33") is False
    assert telemetry.report(RuntimeError("x")) is False

    out, err = capsys.readouterr()
    assert out == ""
    assert "LIUM_SENTRY_DSN" in err and "crash reporting is off" in err
    if "[/x]" in bad_dsn:
        assert "[/x]" in err   # printed as text, not eaten as a tag


def test_a_failing_shipped_dsn_does_not_blame_a_variable_the_user_never_set(monkeypatch, capsys):
    monkeypatch.setenv("LIUM_TELEMETRY", "1")
    monkeypatch.setattr(telemetry, "DEFAULT_SENTRY_DSN", "garbage")

    assert telemetry.init("lium ls", "0.0.33") is False
    err = capsys.readouterr().err
    assert "crash reporting could not start" in err and "LIUM_SENTRY_DSN" not in err


def test_a_malformed_dsn_override_does_not_take_the_command_down(monkeypatch):
    # init() runs in the `lium` group callback, before every subcommand and outside handle_errors: on
    # e8ed5e1 `LIUM_SENTRY_DSN=garbage lium ls --help` died with sentry_sdk.utils.BadDsn, exit 1
    from lium.cli.cli import cli

    monkeypatch.setenv("LIUM_TELEMETRY", "1")
    monkeypatch.setenv("LIUM_SENTRY_DSN", "garbage")

    result = CliRunner().invoke(cli, ["ls", "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage: cli ls" in result.output
    assert "LIUM_SENTRY_DSN" in result.stderr and "LIUM_SENTRY_DSN" not in result.stdout
    assert telemetry._initialised is False


def test_disabled_never_touches_the_sdk(monkeypatch):
    monkeypatch.setenv("LIUM_SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: pytest.fail("SDK initialised while telemetry is off"))

    assert telemetry.init("lium up", "0.0.33") is False


class ListTransport(sentry_sdk.transport.Transport):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


@pytest.fixture
def events(monkeypatch):
    captured = []
    real_init = sentry_sdk.init
    monkeypatch.setenv("LIUM_TELEMETRY", "1")
    monkeypatch.setenv("LIUM_SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: real_init(transport=ListTransport(captured), **kwargs))
    return captured


def _crash_with_secrets_in_scope(pod_name: str, api_key: str):
    home_path = "/Users/renter/.lium/config.ini"
    raise RuntimeError(f"cannot read {home_path} for renter@example.com key {api_key}; pod {pod_name} missing")


def test_report_sends_the_crash_and_the_command_but_not_the_values(events):
    assert telemetry.init("lium up", "0.0.33") is True
    # built at runtime: the SDK attaches source context lines, so a literal here would show up legitimately
    pod_name = "-".join(["my", "secret", "pod"])
    api_key = "sk_" + "".join(chr(ord("A") + i % 26) for i in range(43))

    @click.command("up")
    @click.argument("pod_name")
    @handle_errors
    def up(pod_name):
        _crash_with_secrets_in_scope(pod_name, api_key)

    result = CliRunner().invoke(up, [pod_name])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Unexpected error" in result.output
    assert telemetry.OPT_IN_HINT not in result.output  # already opted in, no nudge
    assert len(events) == 1
    event = events[0]
    assert event["tags"]["command"] == "up"
    assert event["tags"]["python"] and event["tags"]["os"]
    assert event["tags"]["cli_version"] == "0.0.33"
    assert event["tags"]["api_host"] == "lium.io"
    assert event["tags"]["error_class"] == "RuntimeError"
    assert event["release"] == "lium-cli@0.0.33"
    assert event["environment"] == "prod"
    exc = event["exception"]["values"][0]
    assert exc["type"] == "RuntimeError"
    # the pod name travelled inside the message; the argument's value is cut out of it
    assert exc["value"] == "cannot read ~/.lium/config.ini for [email] key [api-key]; pod [arg] missing"
    frames = exc["stacktrace"]["frames"]
    assert frames, "the stack is the point of the report"
    assert all("vars" not in frame for frame in frames)  # local variables held pod_name and the key
    assert all("/Users/" not in frame.get("abs_path", "") for frame in frames)
    for absent in ("request", "user", "breadcrumbs", "server_name", "modules", "extra"):
        assert absent not in event
    serialised = repr(event)
    assert pod_name not in serialised
    assert api_key[3:] not in serialised


def test_windows_home_directories_are_scrubbed_too():
    text = r"C:\Users\renter\AppData\Local\lium\config.ini and c:\users\Renter Two\x"
    assert telemetry.scrub_text(text) == r"~\AppData\Local\lium\config.ini and ~\x"   # the whole "Renter Two" goes
    assert telemetry.scrub_text("C:\\Users\\Renter Two\nnext line") == "~\nnext line"   # home dir last on its line
    assert telemetry.scrub_text(r"'C:\Users\Renter Two' is not writable") == "'~' is not writable"
    assert telemetry.scrub_text(r"C:\Users\O'Brien\lium\cli.py and 'C:\Users\D'Souza' too") == r"~\lium\cli.py and '~' too"
    # an apostrophe followed by a non-ASCII letter is still inside the name
    assert telemetry.scrub_text(r"C:\Users\D'Ávila\x and 'C:\Users\D'Ávila' too") == r"~\x and '~' too"
    # a bare home directory followed by prose and a second path: the second path's `C:` is not swallowed
    text = r"C:\Users\Renter Two not writable; falling back to C:\Users\Renter Two\AppData\Local\Temp"
    assert telemetry.scrub_text(text) == r"~~\AppData\Local\Temp"
    assert telemetry.scrub_text(r"cannot use C:\Users\bob, using C:\Users\bob\.lium instead") == r"cannot use ~~\.lium instead"


def test_the_running_users_home_is_scrubbed_whatever_it_is_called(monkeypatch):
    """A custom Unix home (`/srv/users/alice`) or `/root` is not under /Users or /home, so the spelling
    patterns miss it; `Path.home()` itself is cut out as a path prefix (arhangel66 on #212). `Path.home`
    is patched, not `HOME`: on Windows it reads `USERPROFILE`."""
    monkeypatch.setattr(telemetry.Path, "home", lambda: PurePosixPath("/srv/users/alice"))
    text = "[Errno 13] Permission denied: '/srv/users/alice/.lium/config.ini' (home /srv/users/alice)"
    assert telemetry.scrub_text(text) == "[Errno 13] Permission denied: '~/.lium/config.ini' (home ~)"

    monkeypatch.setattr(telemetry.Path, "home", lambda: PurePosixPath("/root"))
    assert telemetry.scrub_text("/root/.lium/config.ini, /rootfs/etc and /root") == "~/.lium/config.ini, /rootfs/etc and ~"

    # a one-character home is not a name; without the guard '/.lium/config.ini' would read '~.lium/config.ini'
    monkeypatch.setattr(telemetry.Path, "home", lambda: PurePosixPath("/"))
    assert telemetry.scrub_text("[Errno 13] Permission denied: '/.lium/config.ini'") == "[Errno 13] Permission denied: '/.lium/config.ini'"

    # a Windows profile outside Users: any case, backslashes single or doubled by a repr, or forward slashes
    monkeypatch.setattr(telemetry.Path, "home", lambda: PureWindowsPath(r"D:\Profiles\Renter Two"))
    text = r"d:\profiles\renter two\x, D:/Profiles/Renter Two/.lium and 'D:\\Profiles\\Renter Two\\y'"
    assert telemetry.scrub_text(text) == r"~\x, ~/.lium and '~\\y'"


def test_a_windows_path_inside_a_real_oserror_message_is_scrubbed(events):
    """`OSError.__str__` reprs the filename, doubling the backslashes (`'C:\\Users\\Renter Two\\…'`); an
    `open()` failure under `~/.lium` is the likeliest unexpected error that carries a home directory."""
    assert telemetry.init("lium up", "0.0.33") is True
    user = " ".join(["Renter", "Two"])   # built at runtime: the SDK attaches source context lines

    @click.command("up")
    @handle_errors
    def up():
        raise FileNotFoundError(2, "No such file or directory", "C:\\Users\\" + user + "\\.lium\\config.ini")

    assert CliRunner().invoke(up, []).exit_code == EXIT_GENERAL_ERROR
    assert len(events) == 1
    value = events[0]["exception"]["values"][0]["value"]
    assert value == "[Errno 2] No such file or directory: '~\\\\.lium\\\\config.ini'"
    assert user not in repr(events[0])
    for message in (
        str(PermissionError(13, "Permission denied", "C:\\Users\\O'Brien\\.lium")),
        str(FileExistsError(17, "File exists", r"C:\Users\bob\a", 0, r"C:\Users\bob\b")),
        str(KeyError(r"C:\Users\bob\x")),
    ):
        assert "Users" not in telemetry.scrub_text(message), message


def test_scrub_patterns_are_linear_on_long_text_and_the_message_is_capped(monkeypatch):
    """The e-mail pattern's `\b` made scrubbing quadratic on long messages; a 40k-char message must scrub in
    well under a second, and init() caps the value length so nothing longer reaches the patterns."""
    import time

    for text in ("a" * 40000 + " no address", "x@" * 20000, "C:\\Users\\" + "y" * 40000):
        started = time.monotonic()
        telemetry.scrub_text(text)
        assert time.monotonic() - started < 0.5

    seen = {}
    monkeypatch.setenv("LIUM_TELEMETRY", "1")
    monkeypatch.setenv("LIUM_SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    monkeypatch.setattr(telemetry, "_initialised", False)
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: seen.update(kwargs))
    assert telemetry.init("lium up", "0.0.33") is True
    assert seen["max_value_length"] == telemetry.MAX_VALUE_LENGTH   # the cap the docstring names
    assert seen["include_local_variables"] is False


def test_a_long_message_is_capped_before_the_scrubber_sees_it(events, monkeypatch):
    """The SDK serialises (and truncates to max_value_length) before it calls before_send, so a 15,000-char
    message reaches _scrub_event as MAX_VALUE_LENGTH characters ending in `...`."""
    seen = {}
    real_scrub = telemetry._scrub_event

    def spy(event, hint):
        seen["value"] = event["exception"]["values"][0]["value"]
        return real_scrub(event, hint)
    monkeypatch.setattr(telemetry, "_scrub_event", spy)
    assert telemetry.init("lium up", "0.0.33") is True

    @click.command("up")
    @handle_errors
    def up():
        raise RuntimeError("m" * 15000)

    assert CliRunner().invoke(up, []).exit_code == EXIT_GENERAL_ERROR
    assert len(seen["value"]) == telemetry.MAX_VALUE_LENGTH and seen["value"].endswith("...")
    assert events[0]["exception"]["values"][0]["value"] == seen["value"]


def test_windows_frame_paths_are_scrubbed(events):
    assert telemetry.init("lium up", "0.0.33") is True
    event = {
        "exception": {"values": [{"value": "boom", "stacktrace": {"frames": [
            {"abs_path": r"C:\Users\renter\lium\cli.py", "filename": r"C:\Users\renter\lium\cli.py", "vars": {"k": 1}},
        ]}}]},
    }
    out = telemetry._scrub_event(event, None)
    frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert frame["abs_path"] == r"~\lium\cli.py" and frame["filename"] == r"~\lium\cli.py"
    assert "vars" not in frame


def test_a_crash_against_a_staging_api_is_a_staging_event(events, monkeypatch):
    monkeypatch.setenv("LIUM_BASE_URL", "https://staging.lium.io/api")
    assert telemetry.init("lium ps", "0.0.33") is True

    @click.command("ps")
    @handle_errors
    def ps():
        raise KeyError("gpu_count")

    CliRunner().invoke(ps, [])

    assert len(events) == 1
    assert events[0]["environment"] == "staging"
    assert events[0]["tags"]["api_host"] == "staging.lium.io"
    assert events[0]["tags"]["error_class"] == "KeyError"


def test_expected_failures_are_not_crashes(events):
    from lium.sdk import LiumError

    assert telemetry.init("lium ps", "0.0.33") is True

    @click.command("ps")
    @handle_errors
    def ps():
        raise LiumError("balance too low")

    result = CliRunner().invoke(ps, [])

    assert result.exit_code != 0
    assert events == []


def test_unexpected_error_mentions_the_opt_in_when_off():
    @click.command("ps")
    @handle_errors
    def ps():
        raise KeyError("gpu_count")

    result = CliRunner().invoke(ps, [])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert telemetry.OPT_IN_HINT in result.output
