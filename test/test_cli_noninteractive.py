"""The CLI must never wait on a question nobody can answer.

An agent drives the CLI through a pipe. A hidden confirmation prompt there
reads from a stdin nobody writes to, and the command looks hung; a prompt that
hits EOF crashes with a traceback and exit 1. Every prompt in the renter CLI
now goes through one gate: with no terminal on stdin, or with
``LIUM_NONINTERACTIVE=1``, it either takes the documented default or fails at
once with a hint naming the flag to pass.
"""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli import interactive, ui
from lium.cli.cli import cli
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR


@pytest.fixture(autouse=True)
def _unset_noninteractive(monkeypatch):
    monkeypatch.delenv(interactive.NONINTERACTIVE_ENV, raising=False)


def _terminal(monkeypatch, attached: bool) -> None:
    monkeypatch.setattr(interactive, "stdin_is_terminal", lambda: attached)


# --- the gate -----------------------------------------------------------------

def test_a_pipe_is_not_interactive(monkeypatch):
    _terminal(monkeypatch, attached=False)

    assert interactive.is_interactive() is False
    assert "not a terminal" in interactive.noninteractive_reason()


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_env_var_turns_prompting_off_even_on_a_terminal(monkeypatch, value):
    _terminal(monkeypatch, attached=True)
    monkeypatch.setenv(interactive.NONINTERACTIVE_ENV, value)

    assert interactive.is_interactive() is False
    assert interactive.NONINTERACTIVE_ENV in interactive.noninteractive_reason()


def test_a_terminal_without_the_opt_out_is_interactive(monkeypatch):
    _terminal(monkeypatch, attached=True)

    assert interactive.is_interactive() is True


def test_a_closed_stdin_is_not_a_terminal(monkeypatch):
    monkeypatch.setattr(interactive.sys, "stdin", None)

    assert interactive.stdin_is_terminal() is False


# --- ui.confirm / ui.prompt ----------------------------------------------------

def test_confirm_fails_instead_of_asking_when_piped(monkeypatch):
    _terminal(monkeypatch, attached=False)
    monkeypatch.setattr(ui.Confirm, "ask", lambda *a, **k: pytest.fail("prompt was shown"))

    with pytest.raises(CliFailure) as raised:
        ui.confirm("Remove everything?")

    assert raised.value.code == "confirmation_required"
    assert raised.value.exit_code == EXIT_CONFIGURATION_ERROR
    assert "Remove everything?" in raised.value.message
    assert "--yes" in raised.value.message


def test_confirm_names_the_flag_the_caller_gave_it(monkeypatch):
    _terminal(monkeypatch, attached=False)

    with pytest.raises(CliFailure) as raised:
        ui.confirm("Reset?", hint="re-run with --confirm")

    assert "--confirm" in raised.value.message


def test_confirm_still_asks_a_human(monkeypatch):
    _terminal(monkeypatch, attached=True)
    monkeypatch.setattr(ui.Confirm, "ask", lambda message, default=False, **kwargs: True)

    assert ui.confirm("Proceed?") is True


def test_confirm_treats_a_lost_terminal_as_a_failure_not_a_yes(monkeypatch):
    _terminal(monkeypatch, attached=True)

    def _eof(*args, **kwargs):
        raise EOFError()

    monkeypatch.setattr(ui.Confirm, "ask", _eof)

    with pytest.raises(CliFailure) as raised:
        ui.confirm("Proceed?")

    assert raised.value.code == "confirmation_required"


def test_prompt_returns_its_default_when_piped(monkeypatch):
    _terminal(monkeypatch, attached=False)
    monkeypatch.setattr(ui.Prompt, "ask", lambda *a, **k: pytest.fail("prompt was shown"))

    assert ui.prompt("Wallet name", default="default") == "default"


def test_prompt_without_a_default_fails_with_the_option_to_pass(monkeypatch):
    _terminal(monkeypatch, attached=False)

    with pytest.raises(CliFailure) as raised:
        ui.prompt("Amount", hint="pass --amount")

    assert raised.value.code == "input_required"
    assert raised.value.exit_code == EXIT_CONFIGURATION_ERROR
    assert "--amount" in raised.value.message


# --- commands -----------------------------------------------------------------

def _executor() -> SimpleNamespace:
    return SimpleNamespace(
        id="executor-uuid-1", huid="brave-orbit-b9", gpu_type="H100", gpu_count=1,
        price_per_hour=2.0, price_per_gpu=2.0, download_speed=1000,
        location={"country": "US"}, specs={}, docker_in_docker=False,
        max_cuda_version=12.4, tier="secure",
    )


class _UpLium:
    """Rent must never be reached without an answer; ``rented`` records if it was."""

    rented: list = []
    # a server without workspaces: `up` reads it for its workspace line (DAH-3033)
    workspaces = SimpleNamespace(current=lambda: None)

    def __init__(self, *args, **kwargs):
        pass

    def get_executor(self, executor_id):
        return _executor()

    def default_docker_template(self, executor_id):
        return SimpleNamespace(id="tpl-1", name="pytorch")

    def get_deployment_estimate(self, executor_id, template_id):
        return {}

    def up(self, **kwargs):
        _UpLium.rented.append(kwargs)
        return {"id": "pod-uuid-1234", "name": "brave-orbit-b9"}


def test_up_without_yes_fails_fast_when_piped_and_rents_nothing(monkeypatch):
    """The costliest prompt in the CLI: it must fail, not hang, not rent."""
    from lium.cli.up import command as up_module

    _terminal(monkeypatch, attached=False)
    _UpLium.rented = []
    monkeypatch.setattr(up_module, "Lium", _UpLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["up", "some-node-id", "--no-ssh"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "--yes" in result.output
    assert _UpLium.rented == []


def test_up_with_yes_never_looks_for_a_terminal(monkeypatch):
    from lium.cli.up import command as up_module

    _terminal(monkeypatch, attached=False)
    _UpLium.rented = []
    monkeypatch.setattr(up_module, "Lium", _UpLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    monkeypatch.setattr(ui.Confirm, "ask", lambda *a, **k: pytest.fail("prompt was shown"))
    monkeypatch.setattr(
        up_module.WaitReadyAction, "execute",
        lambda self, ctx: SimpleNamespace(ok=True, data={"pod": SimpleNamespace(
            id="pod-uuid-1234", huid="brave-orbit-b9", name="brave-orbit-b9",
            status="RUNNING", ssh_cmd="ssh root@1.2.3.4", ports={},
        )}),
    )

    result = CliRunner().invoke(cli, ["up", "some-node-id", "-y", "--no-ssh"])

    assert result.exit_code == 0, result.output
    assert len(_UpLium.rented) == 1


def test_rm_all_and_rm_by_index_fail_closed_when_piped(monkeypatch):
    """Wiping pods on the strength of a missing prompt is what this module prevents: without --yes a
    piped `rm --all` or `rm <row>` fails with confirmation_required and names the flag; --yes proceeds."""
    from lium.cli.rm import command as rm_module

    _terminal(monkeypatch, attached=True)
    monkeypatch.setenv(interactive.NONINTERACTIVE_ENV, "1")

    with pytest.raises(CliFailure) as failure:
        rm_module.human_approved_removing_every_pod([SimpleNamespace(huid="a")])
    assert failure.value.code == "confirmation_required" and "--yes" in str(failure.value)

    match = SimpleNamespace(index=1, pod=SimpleNamespace(huid="a", name="n", id="id-a"))
    monkeypatch.setattr(rm_module, "describe_index_match", lambda m: "1 → a")
    with pytest.raises(CliFailure) as failure:
        rm_module.human_approved_index_targets([match])
    assert failure.value.code == "confirmation_required"
    assert rm_module.human_approved_index_targets([match], yes=True) is True


def test_volumes_rm_without_yes_fails_fast_when_piped(monkeypatch):
    from lium.cli.volumes.rm import command as volumes_rm_module

    _terminal(monkeypatch, attached=False)
    monkeypatch.setattr(volumes_rm_module, "ensure_config", lambda: None)
    monkeypatch.setattr(
        volumes_rm_module, "get_last_volume_selection",
        lambda: {"volumes": [{"id": "vol-1", "huid": "calm-lake-01", "name": "data"}]},
    )
    monkeypatch.setattr(volumes_rm_module, "Lium", lambda *a, **k: pytest.fail("removal started"))

    result = CliRunner().invoke(cli, ["volumes", "rm", "1"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "--yes" in result.output


def test_config_reset_accepts_yes_and_names_it_when_piped(monkeypatch):
    from lium.cli.config.reset import command as reset_module

    _terminal(monkeypatch, attached=False)
    reset_calls = []
    monkeypatch.setattr(
        reset_module.ResetConfigAction, "execute",
        lambda self, ctx: reset_calls.append(ctx) or SimpleNamespace(ok=True, error=None),
    )

    refused = CliRunner().invoke(cli, ["config", "reset"])
    assert refused.exit_code == EXIT_CONFIGURATION_ERROR
    assert "--yes" in refused.output
    assert reset_calls == []

    accepted = CliRunner().invoke(cli, ["config", "reset", "-y"])
    assert accepted.exit_code == 0, accepted.output
    assert len(reset_calls) == 1


def test_config_set_of_a_secret_without_a_value_fails_instead_of_asking(monkeypatch, tmp_path):
    """`config set api.api_key` with no value opens a password prompt; piped it must not."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    _terminal(monkeypatch, attached=False)
    monkeypatch.setattr(ui.Prompt, "ask", lambda *a, **k: pytest.fail("prompt was shown"))

    result = CliRunner().invoke(cli, ["config", "set", "api.api_key"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "lium config set api.api_key <VALUE>" in result.output


def test_missing_api_key_without_a_terminal_names_the_env_var(monkeypatch):
    """The browser login used to open a browser nobody sees and poll for 30 s."""
    from lium.cli import settings
    from lium.cli.init import actions as init_actions

    _terminal(monkeypatch, attached=False)
    monkeypatch.setattr(settings.config, "get", lambda key, default=None: None)
    monkeypatch.setattr(
        init_actions.SetupApiKeyAction, "execute",
        lambda self, ctx: pytest.fail("browser login started"),
    )

    result = CliRunner().invoke(cli, ["ps"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "LIUM_API_KEY" in result.output
    assert "lium init --no-browser" in result.output


def test_init_without_a_terminal_takes_the_headless_path(monkeypatch):
    from lium.cli.init import command as init_module

    _terminal(monkeypatch, attached=False)
    calls = []
    monkeypatch.setattr(
        init_module.RequestAuthUrlAction, "execute",
        lambda self, ctx: calls.append("url") or SimpleNamespace(ok=True, data={}),
    )
    monkeypatch.setattr(
        init_module.SetupApiKeyAction, "execute",
        lambda self, ctx: pytest.fail("browser login started"),
    )

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert calls == ["url"]


def test_fund_without_an_amount_fails_with_the_flag_when_piped(monkeypatch):
    """The TAO flow asked for the amount on stdin; a script must be told to pass -a."""
    import sys
    import types

    from lium.cli.fund import command as fund_module

    _terminal(monkeypatch, attached=False)
    monkeypatch.setitem(sys.modules, "bittensor", types.ModuleType("bittensor"))

    class _LoadedWallet:
        def execute(self, ctx):
            return SimpleNamespace(ok=True, data={"wallet": object(), "address": "coldkey"}, error=None)

    monkeypatch.setattr(fund_module, "LoadWalletAction", _LoadedWallet)
    monkeypatch.setattr(fund_module, "Lium", lambda *a, **k: SimpleNamespace(balance=lambda: 0.0))

    result = CliRunner().invoke(cli, ["fund", "-w", "default"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "--amount" in result.output


def test_fund_confirmation_is_refused_not_skipped_when_piped(monkeypatch):
    import sys
    import types

    from lium.cli.fund import command as fund_module

    _terminal(monkeypatch, attached=False)
    monkeypatch.setitem(sys.modules, "bittensor", types.ModuleType("bittensor"))

    class _LoadedWallet:
        def execute(self, ctx):
            return SimpleNamespace(ok=True, data={"wallet": object(), "address": "coldkey"}, error=None)

    monkeypatch.setattr(fund_module, "LoadWalletAction", _LoadedWallet)
    monkeypatch.setattr(fund_module, "Lium", lambda *a, **k: SimpleNamespace(balance=lambda: 0.0))
    monkeypatch.setattr(
        fund_module.UnlockColdkeyAction, "execute",
        lambda self, ctx: pytest.fail("transfer flow started without confirmation"),
    )

    result = CliRunner().invoke(cli, ["fund", "-w", "default", "-a", "1.5"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "--yes" in result.output


def test_backup_param_prompts_fall_back_to_defaults_when_piped(monkeypatch):
    from lium.cli import utils

    _terminal(monkeypatch, attached=False)
    monkeypatch.setattr(utils.Prompt, "ask", lambda *a, **k: pytest.fail("prompt was shown"))

    params = utils.ensure_backup_params(enabled=True)

    assert params.path == utils.config.default_backup_path
    assert params.frequency == utils.config.default_backup_frequency
