"""DAH-2556: the CLI's contract with a non-interactive caller.

An autonomous agent drives the CLI through a pipe: it cannot read Rich-formatted
hints, it cannot answer a prompt, and the only two things it can act on are the
process exit code and what lands on stdout. Today a failed remote command loses
its stdout and still exits 0, `rm` rejects the `-y` an agent learned on `up`,
and an explicit `--sort` is overridden by the Pareto star, so "give me the
cheapest node" hands back the most expensive one.
"""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.commands import exec as exec_module
from lium.cli.ls import command as ls_command_module
from lium.cli.ls import display as ls_display
from lium.cli.ps import command as ps_module
from lium.cli.reboot import command as reboot_module
from lium.cli.rsync import command as rsync_module
from lium.cli.rm import command as rm_module
from lium.cli.utils import (
    EXIT_API_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_PERMISSION_DENIED,
    EXIT_SSH_ERROR,
    EXIT_POD_NOT_FOUND,
)
from lium.sdk import LiumPermissionError, LiumServerError


def _pod(huid: str = "eager-wolf-aa", name: str = "my-pod") -> SimpleNamespace:
    return SimpleNamespace(id="pod-uuid-1", huid=huid, name=name)


def _executor(huid: str, price_per_hour: float, download: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id-{huid}",
        huid=huid,
        gpu_type="RTX4090",
        gpu_count=1,
        price_per_hour=price_per_hour,
        price_per_gpu=price_per_hour,
        location={"country": "Ukraine", "country_code": "UA"},
        download_speed=download,
        upload_speed=download,
        specs={"network": {"download_speed": download, "upload_speed": download}},
        docker_in_docker=False,
        max_cuda_version=12.4,
        tier="secure",
    )


# `Lium.workspaces` on a server without workspaces: `ps`, `ls`, `up` and `rm` read it for their workspace line
_NO_WORKSPACES = SimpleNamespace(current=lambda: None)


class _FakeLium:
    """Stands in for the SDK: one pod, and an exec result the test dictates."""

    result: dict[str, object] = {}
    workspaces = _NO_WORKSPACES

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return [_pod()]

    def exec(self, pod, command=None, env=None):
        return dict(self.result)


def _run_exec(
    monkeypatch,
    result: dict[str, object],
    extra_args: list[str] | None = None,
    target: str = "my-pod",
):
    _FakeLium.result = result
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)
    return CliRunner().invoke(cli, ["exec", target, "echo hi", *(extra_args or [])])


def _run_rm(monkeypatch, target: str = "my-pod"):
    _FakeRmLium.removed = []
    monkeypatch.setattr(rm_module, "Lium", _FakeRmLium)
    return CliRunner().invoke(cli, ["rm", target, "-y"])


def test_exec_keeps_stdout_when_the_remote_command_fails(monkeypatch):
    """The log an agent needs to diagnose a crash must survive the crash."""
    result = _run_exec(
        monkeypatch,
        {"success": False, "exit_code": 3, "stdout": "VISIBLE_STDOUT\n", "stderr": ""},
    )

    assert "VISIBLE_STDOUT" in result.output


def test_exec_exits_with_the_remote_exit_code(monkeypatch):
    """`lium exec ... && next` must not run `next` after a failed command."""
    result = _run_exec(
        monkeypatch,
        {"success": False, "exit_code": 3, "stdout": "", "stderr": "boom\n"},
    )

    assert result.exit_code == 3


def test_exec_exits_zero_when_the_remote_command_succeeds(monkeypatch):
    result = _run_exec(
        monkeypatch,
        {"success": True, "exit_code": 0, "stdout": "ok\n", "stderr": ""},
    )

    assert result.exit_code == 0
    assert "ok" in result.output


def test_exec_json_carries_stdout_stderr_and_exit_code(monkeypatch):
    result = _run_exec(
        monkeypatch,
        {"success": False, "exit_code": 7, "stdout": "out\n", "stderr": "err\n"},
        ["--json"],
    )

    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["results"] == [
        {"pod": "eager-wolf-aa", "stdout": "out\n", "stderr": "err\n", "exit_code": 7, "error": None}
    ]
    assert result.exit_code == 7


def test_exec_fails_loudly_when_no_pod_matches(monkeypatch):
    """A typo must not read as a successful run on a pod that was never touched."""
    result = _run_exec(
        monkeypatch,
        {"success": True, "exit_code": 0, "stdout": "", "stderr": ""},
        target="no-such-pod-zz",
    )

    assert result.exit_code == EXIT_POD_NOT_FOUND


class _FakeRmLium:
    removed: list[str] = []
    workspaces = _NO_WORKSPACES

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return [_pod()]

    def rm(self, pod):
        _FakeRmLium.removed.append(pod.huid)


def test_rm_accepts_yes_and_reports_what_it_removed(monkeypatch):
    """Agents learn `-y` on `up`, and silence on teardown reads like a no-op."""
    result = _run_rm(monkeypatch)

    assert result.exit_code == 0
    assert _FakeRmLium.removed == ["eager-wolf-aa"]
    assert "eager-wolf-aa" in result.output


def test_rm_fails_loudly_when_no_pod_matches(monkeypatch):
    """`lium rm <typo>` must not look exactly like a successful termination."""
    result = _run_rm(monkeypatch, target="no-such-pod-zz")

    assert result.exit_code == EXIT_POD_NOT_FOUND
    assert _FakeRmLium.removed == []


@pytest.mark.parametrize("sort_by", ["price_total", "price_per_hour"])
def test_explicit_sort_is_not_overridden_by_the_pareto_star(sort_by):
    """"Cheapest first" must mean cheapest first, star or no star."""
    cheap = _executor("cheap-node", price_per_hour=0.30, download=10)
    expensive_but_starred = _executor("starred-node", price_per_hour=64.00, download=9999)

    ordered, _ = ls_display.sort_executors([cheap, expensive_but_starred], sort_by=sort_by)

    assert ordered[0].huid == "cheap-node"


@pytest.mark.parametrize("sort_by", ["price_total", "price_per_hour"])
def test_cheapest_sort_survives_the_whole_ls_command(monkeypatch, sort_by):
    """End to end: the option a caller types must reach the sort it names."""
    cheap = _executor("cheap-node", price_per_hour=0.30, download=10)
    expensive_but_starred = _executor("starred-node", price_per_hour=64.00, download=9999)

    class _FakeLsLium:
        def __init__(self, *args, **kwargs):
            pass

        def ls(self, **kwargs):
            return [expensive_but_starred, cheap]

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLsLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)

    result = CliRunner().invoke(cli, ["ls", "--sort", sort_by, "--format", "json"])

    assert result.exit_code == 0, result.output
    assert [row["huid"] for row in json.loads(result.output)][0] == "cheap-node"


def test_default_ls_ordering_puts_the_cheapest_node_first(monkeypatch):
    """Without --sort the cheapest $/GPU·h leads; the ★ marks, it does not rank (DAH-3079)."""
    cheap = _executor("cheap-node", price_per_hour=0.30, download=10)
    expensive_but_starred = _executor("starred-node", price_per_hour=64.00, download=9999)

    class _FakeLsLium:
        def __init__(self, *args, **kwargs):
            pass

        def ls(self, **kwargs):
            return [cheap, expensive_but_starred]

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLsLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)

    result = CliRunner().invoke(cli, ["ls", "--format", "json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [row["huid"] for row in rows] == ["cheap-node", "starred-node"]
    assert [row["is_pareto"] for row in rows] == [False, True]


def test_exec_script_flag_reads_the_file(monkeypatch, tmp_path):
    """--script is the documented way to send a multi-line job; it must work."""
    script = tmp_path / "job.sh"
    script.write_text("echo from-script\n")
    _FakeLium.result = {"success": True, "exit_code": 0, "stdout": "ran\n", "stderr": ""}
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["exec", "my-pod", "--script", str(script)])

    assert result.exit_code == 0, result.output


def test_exec_config_errors_stay_json_under_json(monkeypatch):
    """A machine caller must get the envelope on every failure, not just some."""
    _FakeLium.result = {"success": True, "exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["exec", "my-pod", "-e", "NOT_A_PAIR", "cmd", "--json"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert json.loads(result.stderr)["ok"] is False


def test_exec_stderr_carries_no_rich_markup(monkeypatch):
    """The command's own stderr must reach the caller unstyled."""
    result = _run_exec(
        monkeypatch,
        {"success": False, "exit_code": 3, "stdout": "", "stderr": "boom\n"},
    )

    assert "boom" in result.output
    assert "[/]" not in result.output


def test_rm_named_pod_on_an_empty_account_fails(monkeypatch):
    """A typo must fail even when the account happens to hold no pods."""

    class _EmptyLium:
        workspaces = _NO_WORKSPACES

        def __init__(self, *args, **kwargs):
            pass

        def ps(self):
            return []

    monkeypatch.setattr(rm_module, "Lium", _EmptyLium)

    result = CliRunner().invoke(cli, ["rm", "no-such-pod-zz", "-y"])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_rm_all_on_an_empty_account_is_a_no_op(monkeypatch):
    """Removing everything when there is nothing is success, not failure."""

    class _EmptyLium:
        workspaces = _NO_WORKSPACES

        def __init__(self, *args, **kwargs):
            pass

        def ps(self):
            return []

    monkeypatch.setattr(rm_module, "Lium", _EmptyLium)

    result = CliRunner().invoke(cli, ["rm", "--all", "-y"])

    assert result.exit_code == 0


def test_exec_missing_script_still_speaks_json(monkeypatch):
    """A bad --script path must not fall out as click usage text under --json."""
    _FakeLium.result = {"success": True, "exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)

    result = CliRunner().invoke(
        cli, ["exec", "my-pod", "--script", "/no/such/file.sh", "--json"]
    )

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert json.loads(result.stderr)["error"]["code"] == "unreadable_script"


def test_exec_json_keeps_stdout_clean_when_the_api_fails(monkeypatch):
    """stdout belongs to the JSON consumer; the progress spinner must stay off it."""

    class _BrokenLium:
        def __init__(self, *args, **kwargs):
            pass

        def ps(self):
            raise RuntimeError("api down")

    monkeypatch.setattr(exec_module, "Lium", _BrokenLium)

    result = CliRunner().invoke(cli, ["exec", "my-pod", "echo hi", "--json"])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert json.loads(result.stderr)["ok"] is False


def test_up_fails_when_ssh_is_unavailable(monkeypatch):
    """A pod that is rented but unreachable must not report success — DAH-2556."""
    from lium.cli.up import actions as up_actions
    from lium.cli.utils import CliFailure, EXIT_SSH_ERROR

    def _no_ssh(pod_name):
        raise CliFailure(
            "ssh_unavailable",
            f"No SSH connection available for pod '{pod_name}'",
            EXIT_SSH_ERROR,
        )

    monkeypatch.setattr("lium.cli.ssh.command.get_ssh_method_and_pod", _no_ssh)

    with pytest.raises(CliFailure) as raised:
        up_actions.PrepareSSHAction().execute({"pod_name": "brave-orbit-b9"})

    assert raised.value.exit_code == EXIT_SSH_ERROR
    assert "brave-orbit-b9" in raised.value.message


@pytest.mark.parametrize(
    "returncode, connected", [(0, True), (3, True), (255, False)]
)
def test_ssh_session_connected_reports_only_connection_failure(monkeypatch, returncode, connected):
    """A remote shell exiting non-zero is the user's business; 255 is ours."""
    from lium.cli.ssh import command as ssh_module

    monkeypatch.setattr(
        ssh_module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=returncode)
    )

    assert ssh_module.ssh_session_connected(["ssh", "root@203.0.113.7"]) is connected


def test_rm_all_treats_a_lost_terminal_as_no(monkeypatch):
    """No answer is not a yes — an EOF at the prompt must not wipe the account."""
    monkeypatch.setattr(rm_module.ui, "is_interactive", lambda: True)  # rm asks through ui.confirm
    monkeypatch.setattr(rm_module.ui, "confirm", lambda message, **kw: (_ for _ in ()).throw(EOFError()))

    assert rm_module.human_approved_removing_every_pod([_pod()]) is False


def _run_ps_raising(monkeypatch, error: Exception):
    class _RaisingLium:
        def __init__(self, *args, **kwargs):
            pass

        def ps(self):
            raise error

    monkeypatch.setattr(ps_module, "Lium", _RaisingLium)
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)
    return CliRunner().invoke(cli, ["ps"])


def test_permission_denied_exits_with_its_own_code(monkeypatch):
    """An unverified account is a fixable state, not a generic failure."""
    result = _run_ps_raising(monkeypatch, LiumPermissionError("User is not verified"))

    assert result.exit_code == EXIT_PERMISSION_DENIED


def test_other_api_failures_exit_with_the_api_code(monkeypatch):
    """Everything the API rejects for another reason is an API error, not code 1."""
    result = _run_ps_raising(monkeypatch, LiumServerError("Server error: 502"))

    assert result.exit_code == EXIT_API_ERROR


@pytest.mark.parametrize("extra_args", [[], ["--format", "json"]])
def test_ps_named_target_that_matches_nothing_fails(monkeypatch, extra_args):
    """Asking for one pod by name and getting none is a miss in every format."""
    monkeypatch.setattr(ps_module, "Lium", _FakeLium)
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["ps", "no-such-pod-zz", *extra_args])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_port_forward_to_a_missing_pod_fails(monkeypatch):
    """A tunnel that was never opened must not report success to its caller."""
    from lium.cli.port_forward import command as port_forward_module

    monkeypatch.setattr(port_forward_module, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["port-forward", "no-such-pod-zz", "8000"])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_legacy_fund_reports_a_wallet_it_could_not_load(monkeypatch):
    """`lium fund` without --alpha is the default path, not dead code."""
    from lium.cli.fund import command as fund_module

    class _FailingLoadWallet:
        def execute(self, ctx):
            return ActionResult(ok=False, data={}, error="Keyfile not found")

    monkeypatch.setattr(fund_module, "LoadWalletAction", _FailingLoadWallet)

    result = CliRunner().invoke(cli, ["fund", "-w", "default", "-a", "1.5", "-y"])

    # An unreadable keyfile is a local setup problem, not the API refusing.
    assert result.exit_code == EXIT_CONFIGURATION_ERROR


def _run_up_past_the_rent(monkeypatch, extra_args, break_on):
    """Rent succeeds, then the named SDK call fails — the pod is already billing."""
    from lium.cli.up import command as up_module

    class _RentingLium:
        workspaces = _NO_WORKSPACES

        def __init__(self, *args, **kwargs):
            pass

        def get_executor(self, executor_id):
            return _executor("brave-orbit-b9", price_per_hour=1.0, download=100)

        def default_docker_template(self, executor_id):
            return SimpleNamespace(id="tpl-1", name="pytorch")

        def get_deployment_estimate(self, executor_id, template_id):
            return {}

        def up(self, **kwargs):
            return {"id": "pod-uuid-1234", "name": "brave-orbit-b9"}

        def ps(self):
            if break_on == "ps":
                raise LiumServerError("Server error: 502")
            return [SimpleNamespace(
                id="pod-uuid-1234", huid="brave-orbit-b9", name="brave-orbit-b9",
                status="RUNNING", ssh_cmd="ssh root@1.2.3.4",
                # a second port, or InstallJupyterAction fails before it reaches the SDK
                ports={"22": 10022, "8888": 18888},
            )]

        def schedule_termination(self, pod, termination_time=None):
            raise LiumServerError("Server error: 502")

        def install_jupyter(self, pod, jupyter_internal_port=None):
            raise LiumServerError("Server error: 502")

    monkeypatch.setattr(up_module, "Lium", _RentingLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    return CliRunner().invoke(cli, ["up", "some-node-id", "-y", "--no-ssh", *extra_args])


@pytest.mark.parametrize(
    "extra_args, break_on",
    [([], "ps"), (["--ttl", "1h"], "schedule"), (["--jupyter"], "jupyter")],
)
def test_up_names_the_pod_it_already_rented_when_a_later_step_fails(
    monkeypatch, extra_args, break_on
):
    """Past the rent the pod is billing: a failure that hides its id is unusable."""
    result = _run_up_past_the_rent(monkeypatch, extra_args, break_on)

    assert result.exit_code != 0
    assert "pod-uuid-1234" in result.output or "brave-orbit-b9" in result.output


def test_up_reports_an_api_failure_while_resolving_a_node(monkeypatch):
    """`up` used to flatten every SDK error into a generic exit 1."""
    from lium.cli.up import command as up_module

    class _BrokenUpLium:
        workspaces = _NO_WORKSPACES

        def __init__(self, *args, **kwargs):
            pass

        def get_executor(self, executor_id):
            raise LiumServerError("Server error: 502")

    monkeypatch.setattr(up_module, "Lium", _BrokenUpLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["up", "some-node-id", "-y"])

    assert result.exit_code == EXIT_API_ERROR


def test_ls_reports_a_failed_market_listing(monkeypatch):
    """"No nodes" and "the market is unreachable" must not look identical."""
    class _BrokenLsLium:
        def __init__(self, *args, **kwargs):
            pass

        def ls(self, **kwargs):
            raise LiumServerError("Server error: 502")

    monkeypatch.setattr(ls_command_module, "Lium", _BrokenLsLium)

    result = CliRunner().invoke(cli, ["ls"])

    assert result.exit_code == EXIT_API_ERROR


def test_ssh_to_a_missing_pod_fails(monkeypatch):
    """The ticket's own example: `lium ssh <typo>` printed a warning and exited 0."""
    from lium.cli.ssh import command as ssh_module

    monkeypatch.setattr(ssh_module, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["ssh", "no-such-pod-zz"])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_ssh_separates_its_own_failure_from_the_remote_shell(monkeypatch):
    """255 is ssh failing to connect; anything else is the remote's own status."""
    from lium.cli.ssh import actions as ssh_actions
    from lium.cli.ssh import command as ssh_module

    class _SshLium:
        def __init__(self, *a, **kw):
            pass

        def ps(self):
            return [SimpleNamespace(
                id="pod-uuid-1", huid="eager-wolf-aa", name="my-pod",
                status="RUNNING", ssh_cmd="ssh root@203.0.113.7",
            )]

        def ssh_argv(self, pod):
            return ["ssh", "root@203.0.113.7"]

    monkeypatch.setattr(ssh_module, "Lium", _SshLium)

    def _returncode(code):
        monkeypatch.setattr(
            ssh_actions.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code)
        )
        return CliRunner().invoke(cli, ["ssh", "my-pod"])

    assert _returncode(255).exit_code == EXIT_SSH_ERROR
    assert _returncode(3).exit_code == 0


def test_volumes_rm_without_a_cached_listing_fails(monkeypatch):
    """"Run `lium volumes` first" is an instruction, not a completed removal."""
    from lium.cli.volumes.rm import command as volumes_rm_module

    monkeypatch.setattr(volumes_rm_module, "ensure_config", lambda: None)
    monkeypatch.setattr(volumes_rm_module, "get_last_volume_selection", lambda: None)

    result = CliRunner().invoke(cli, ["volumes", "rm", "1", "-y"])

    assert result.exit_code == EXIT_GENERAL_ERROR


def test_rsync_reports_a_pod_it_could_not_reach(tmp_path, monkeypatch):
    """A batch copy that skipped a pod must not read as "everything is in place"."""
    class _RsyncLium:
        def __init__(self, *a, **kw):
            pass

        def ps(self):
            return [_pod()]

        def rsync(self, *a, **kw):
            raise LiumServerError("Server error: 502")

    monkeypatch.setattr(rsync_module, "Lium", _RsyncLium)
    local = tmp_path / "payload"
    local.mkdir()

    result = CliRunner().invoke(cli, ["rsync", "all", str(local)])

    assert result.exit_code == EXIT_GENERAL_ERROR


def _run_reboot(monkeypatch, args, pods):
    class _RebootLium:
        def __init__(self, *a, **kw):
            pass

        def ps(self):
            return pods

        def reboot(self, pod, volume_id=None):
            raise LiumServerError("Server error: 502")

    monkeypatch.setattr(reboot_module, "Lium", _RebootLium)
    return CliRunner().invoke(cli, ["reboot", *args])


def test_reboot_of_a_missing_pod_fails(monkeypatch):
    """A named target that matches nothing is the `rm` asymmetry's failing half."""
    result = _run_reboot(monkeypatch, ["no-such-pod-zz"], [])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_reboot_all_on_an_empty_account_is_a_no_op(monkeypatch):
    """`--all` says "whatever is there" — nothing there is the requested state."""
    result = _run_reboot(monkeypatch, ["--all"], [])

    assert result.exit_code == 0


def test_reboot_reports_a_failed_item_after_finishing_the_batch(monkeypatch):
    """Batch commands run to the end, then fail once — silence would hide it."""
    result = _run_reboot(monkeypatch, ["--all"], [_pod()])

    assert result.exit_code == EXIT_GENERAL_ERROR


def test_bk_show_of_a_missing_pod_fails(monkeypatch):
    """The whole bk family names one pod — a miss is a miss, not a silent skip."""
    from lium.cli.bk.show import command as bk_show_module

    monkeypatch.setattr(bk_show_module, "Lium", _FakeLium)
    monkeypatch.setattr(bk_show_module, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["bk", "show", "no-such-pod-zz"])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_config_get_of_a_missing_key_fails(monkeypatch):
    """`config get` is how a script reads state — a miss must not look like a hit."""
    from lium.cli.config.get import actions as config_get_actions

    monkeypatch.setattr(config_get_actions.config, "get", lambda key, default=None: None)

    result = CliRunner().invoke(cli, ["config", "get", "api.api_key"])

    assert result.exit_code == EXIT_GENERAL_ERROR


def test_config_setup_failure_stops_the_command(monkeypatch):
    """ensure_config() gates ~15 commands — a failed setup must not read as ready."""
    from lium.cli import settings
    from lium.cli.init import actions as init_actions

    monkeypatch.setattr(settings.config, "get", lambda key, default=None: None)

    class _FailingSetup:
        def execute(self, ctx):
            return ActionResult(ok=False, data={}, error="Invalid API key")

    monkeypatch.setattr(init_actions, "SetupApiKeyAction", _FailingSetup)

    result = CliRunner().invoke(cli, ["ps"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR


def test_missing_api_key_points_a_new_user_at_signup(monkeypatch):
    """A first-time user has no account, so 'run lium init' alone sends them to a login they cannot do."""
    from lium.cli import balance as balance_module

    class _NoKeyLium:
        def __init__(self, *args, **kwargs):
            raise ValueError("No API key found. Set LIUM_API_KEY or ~/.lium/config.ini")

    monkeypatch.setattr(balance_module, "Lium", _NoKeyLium)

    result = CliRunner().invoke(cli, ["balance"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "lium init" in result.output
    assert "lium signup --email" in result.output


def test_missing_api_key_in_a_pipe_points_a_new_user_at_signup(monkeypatch):
    """ensure_config()'s non-interactive no_api_key carries its own hint; it must name signup too."""
    from lium.cli import settings, utils as utils_module

    monkeypatch.setattr(settings.config, "get", lambda key, default=None: None)
    monkeypatch.setattr(utils_module, "is_interactive", lambda: False)

    result = CliRunner().invoke(cli, ["ps", "--format", "json"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    envelope = json.loads(result.output)
    assert envelope["error"]["code"] == "no_api_key"
    assert "lium signup --email" in envelope["error"]["hint"]
    assert "lium init --api-key <key>" in envelope["error"]["hint"]   # DAH-3242: the headless path is a hint, not only message text
    assert "--api-key" not in envelope["error"]["message"]           # the message is the diagnosis; the hint is the fix, once


def test_ps_empty_account_is_not_a_failure(monkeypatch):
    """Nothing rented is a legitimate state, not an error."""
    class _EmptyLium(_FakeLium):
        def ps(self):
            return []

    monkeypatch.setattr(ps_module, "Lium", _EmptyLium)
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["ps"])

    assert result.exit_code == 0


def test_a_failure_under_the_spinner_is_reported_once(monkeypatch):
    """The spinner printed its own line, so one failure read as two problems."""
    class _BrokenLsLium:
        def __init__(self, *args, **kwargs):
            pass

        def ls(self, **kwargs):
            raise LiumServerError("Server error: 502")

    monkeypatch.setattr(ls_command_module, "Lium", _BrokenLsLium)

    result = CliRunner().invoke(cli, ["ls"])

    assert result.exit_code == EXIT_API_ERROR
    assert result.output.count("502") == 1


def test_port_forward_lists_the_ports_it_has_below_the_error(monkeypatch):
    """The hint explains the error, so it must not be printed above it."""
    from lium.cli.port_forward import command as port_forward_module

    class _PodWithPorts(_FakeLium):
        def ps(self):
            pod = _pod()
            pod.status = "running"
            pod.ports = {"22": 30022}
            pod.executor = SimpleNamespace(ip="1.2.3.4")
            return [pod]

    monkeypatch.setattr(port_forward_module, "Lium", _PodWithPorts)

    result = CliRunner().invoke(cli, ["port-forward", "my-pod", "8000"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert result.output.index("not exposed") < result.output.index("Available internal ports")
