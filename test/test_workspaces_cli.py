"""`lium workspaces`, `--workspace`, `lium keys`, `Lium.workspaces`, `PodInfo.workspace_id` (lium-platform
DAH-2975 / DAH-2986 / DAH-3030 / DAH-3031).

HTTP is answered from the recorded fixtures in test/fixtures/workspaces with `responses`, so the real SDK
and CLI code paths run. The switch-off cases pin today's behaviour: a server whose GET /users/me has no
`workspace` gets today's output, plus that one `GET /users/me` under `ps` / `ls` tables and before `up` / `rm`
(`--format json` paths make no extra request).
"""

import json
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ps import display as ps_display
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR
from lium.sdk import Config, Lium
from lium.sdk.exceptions import LiumAuthError, LiumError
from lium.sdk.workspaces import NEEDS_SESSION, NOT_ENABLED

FIXTURES = Path(__file__).parent / "fixtures" / "workspaces"
API = "https://lium.io/api"
RESEARCH = "9d8c7b6a-5f4e-4d3c-8b2a-190807060504"
PERSONAL = "11111111-2222-4333-8444-555555555555"
BEN = "7a9e2b1c-3d4f-4e5a-8b6c-1d2e3f4a5b6c"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty ~/.lium with one API key and an ssh key path, so ensure_config() never prompts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    monkeypatch.delenv("LIUM_API_API_KEY", raising=False)  # read before LIUM_API_KEY since lium#136
    # the root option exports LIUM_WORKSPACE for the process and CliRunner does not undo that: pin it to
    # "" (click and Config.load read the empty string as unset) so monkeypatch restores it after each test
    monkeypatch.setenv("LIUM_WORKSPACE", "")
    monkeypatch.delenv("LIUM_SESSION_TOKEN", raising=False)
    (tmp_path / ".lium").mkdir()
    (tmp_path / ".lium" / "config.ini").write_text("[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n")
    # the CLI's settings singleton read the real file at import; point it at this one
    from lium.cli import settings

    monkeypatch.setattr(settings.config, "config_dir", tmp_path / ".lium")
    monkeypatch.setattr(settings.config, "config_file", tmp_path / ".lium" / "config.ini")
    monkeypatch.setattr(settings.config, "_config", settings.config._load_config())
    return tmp_path


def config_text(home) -> str:
    return (home / ".lium" / "config.ini").read_text()


def write_config(home, text: str) -> None:
    """Replace config.ini and reload the CLI's settings singleton (it read the file when `home` was set up)."""
    from lium.cli import settings

    (home / ".lium" / "config.ini").write_text(text)
    settings.config._config = settings.config._load_config()


def me(name="users_me_research"):
    responses.add(responses.GET, f"{API}/users/me", json=fixture(name))


def run(*args, **kwargs):
    return CliRunner().invoke(cli, list(args), **kwargs)


# ------------------------------------------------------------------------------------------------- SDK
@responses.activate
def test_sdk_reads_the_workspace_a_key_acts_in_from_users_me():
    me()
    lium = Lium(Config(api_key="k"))

    current = lium.workspaces.current()

    assert lium.workspaces.enabled() is True
    assert (current.id, current.name, current.role, current.is_personal) == (RESEARCH, "Research", "owner", False)
    assert len(responses.calls) == 1  # cached: the capability check does not call twice


@responses.activate
def test_sdk_sees_no_workspaces_on_a_server_without_them():
    me("users_me_off")
    lium = Lium(Config(api_key="k"))

    assert lium.workspaces.enabled() is False and lium.workspaces.current() is None
    with pytest.raises(LiumError, match=NOT_ENABLED):
        lium.workspaces.require_enabled()


@responses.activate
def test_sdk_reads_with_the_key_and_writes_with_a_session():
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))
    responses.add(responses.GET, f"{API}/workspaces/{RESEARCH}/members", json=fixture("members"))
    responses.add(responses.POST, f"{API}/users/login", json=fixture("login"))
    responses.add(responses.POST, f"{API}/workspaces", json=fixture("workspaces_session")[1])
    responses.add(responses.POST, f"{API}/keys", json=fixture("key_created"))
    lium = Lium(Config(api_key="k"))

    listed = lium.workspaces.list()
    members = lium.workspaces.members(RESEARCH)
    with pytest.raises(LiumAuthError, match="browser session"):
        lium.workspaces.create("Nope")
    token = lium.workspaces.login("ana@example.com", "pw")
    created = lium.workspaces.create("Research")
    key = lium.workspaces.create_key("ci", RESEARCH)

    assert [w.name for w in listed] == ["Research"] and [m.role for m in members] == ["owner", "admin", "member"]
    key_calls = [c for c in responses.calls if c.request.url.endswith("/workspaces") and c.request.method == "GET"]
    assert key_calls[0].request.headers["X-API-KEY"] == "k" and "Authorization" not in key_calls[0].request.headers
    login = next(c for c in responses.calls if c.request.url.endswith("/users/login"))
    assert "X-API-KEY" not in login.request.headers and json.loads(login.request.body)["email"] == "ana@example.com"
    assert token == "eyJ.fixture.session" and created.name == "Research"
    post_key = next(c for c in responses.calls if c.request.url.endswith("/keys"))
    # the key is minted with the session, in the named workspace, and never with the API key header
    assert post_key.request.headers["Authorization"] == "Bearer eyJ.fixture.session"
    assert post_key.request.headers["X-Lium-Workspace-Id"] == RESEARCH and "X-API-KEY" not in post_key.request.headers
    assert key["workspace_id"] == RESEARCH


@responses.activate
def test_sdk_pod_info_carries_workspace_id_and_none_without_workspaces():
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_off"))
    lium = Lium(Config(api_key="k"))

    with_workspace = lium.ps()[0]
    without = lium.ps()[0]

    assert with_workspace.workspace_id == RESEARCH and without.workspace_id is None
    assert "workspace_id" in ps_display.compact_pod(with_workspace)
    assert "workspace_id" not in ps_display.compact_pod(without)  # today's JSON, byte for byte


def test_config_picks_the_key_of_the_requested_or_active_workspace(home, monkeypatch):
    (home / ".lium" / "config.ini").write_text(
        "[api]\napi_key = sk_test_default\n[workspaces]\nactive = research\n"
        "[workspace.research]\nid = 9d8c\napi_key = sk_test_research\n"
    )

    default = Config.load()
    explicit = Config.load(workspace="research")
    monkeypatch.setenv("LIUM_API_KEY", "sk_test_env")
    env_key = Config.load()
    explicit_over_env = Config.load(workspace="research")
    monkeypatch.setenv("LIUM_API_API_KEY", "sk_test_section_env")
    section_env_key = Config.load()
    explicit_over_section_env = Config.load(workspace="research")

    assert (default.api_key, default.workspace) == ("sk_test_research", "research")  # `lium workspaces use`
    assert explicit.api_key == "sk_test_research"
    assert (env_key.api_key, env_key.workspace) == ("sk_test_env", "research")  # env beats the stored default
    assert explicit_over_env.api_key == "sk_test_research"  # an explicit --workspace beats env
    # LIUM_API_API_KEY is the CLI's first env key: it beats LIUM_API_KEY and the stored default, not --workspace
    assert (section_env_key.api_key, section_env_key.workspace) == ("sk_test_section_env", "research")
    assert explicit_over_section_env.api_key == "sk_test_research"
    # an explicit workspace with no saved key never falls through to another key (another team, another balance)
    with pytest.raises(ValueError, match="No API key is saved for workspace 'nowhere'.*--workspace nowhere --save"):
        Config.load(workspace="nowhere")
    monkeypatch.setenv("LIUM_WORKSPACE", "nowhere")
    with pytest.raises(ValueError, match="nowhere"):
        Config.load()


def test_config_without_any_workspace_section_is_todays(home):
    config = Config.load()

    assert (config.api_key, config.workspace, config.session_token) == ("sk_test_default", None, None)


# ------------------------------------------------------------------------------------------------- CLI
@responses.activate
def test_workspaces_list_with_a_key_shows_its_workspace_and_how_to_see_the_rest(home):
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))

    result = run("workspaces")

    assert result.exit_code == 0, result.output
    assert "Research" in result.output and "owner" in result.output and "*" in result.output
    assert "lium workspaces login" in result.output


@responses.activate
def test_workspaces_list_json_with_a_session_lists_every_workspace(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))

    result = run("workspaces", "list", "--json")

    assert result.exit_code == 0, result.output
    assert [w["name"] for w in json.loads(result.output)] == ["Ana's workspace", "Research"]
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer eyJ.fixture.session"


@responses.activate
def test_workspaces_say_when_the_server_has_none_and_everything_else_is_unchanged(home):
    me("users_me_off")
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_off"))

    workspaces = run("workspaces")
    ps = run("ps", "--format", "json")

    assert workspaces.exit_code == EXIT_API_ERROR and NOT_ENABLED in workspaces.output
    assert ps.exit_code == 0, ps.output
    assert "workspace_id" not in ps.output and "Workspace" not in ps.output
    # `ps --format json` makes its one request and no more: the context line is table-only
    assert [c.request.url.rsplit("/", 1)[-1] for c in responses.calls] == ["me", "pods"]


@responses.activate
def test_ps_names_the_workspace_under_the_table_and_in_json(home):
    me()
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))

    table = run("ps")
    as_json = run("ps", "--format", "json")

    assert table.exit_code == 0, table.output
    assert "Workspace: Research (owner)" in table.output
    assert json.loads(as_json.output)[0]["workspace_id"] == RESEARCH


@responses.activate
def test_ps_says_when_the_workspace_could_not_be_read_and_the_listing_stands(home):
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))
    responses.add(
        responses.GET,
        f"{API}/users/me",
        status=403,
        json={"detail": "API key 'ci' belongs to a workspace that is gone or that its account has left"},
    )

    result = run("ps")
    text = " ".join(result.output.split())  # the 80-column console wraps the long warning

    assert result.exit_code == 0, result.output
    assert "Pods (1 active)" in text and "RUNNING" in text  # the listing came out as usual
    assert "Workspace not shown" in text and "belongs to a workspace that is gone" in text
    assert "Run `lium workspaces`" in text
    assert "Workspace:" not in text  # no half-read line


@responses.activate
def test_workspace_flag_picks_the_saved_key_and_refuses_when_none_is_saved(home, monkeypatch):
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research\n",
    )
    me()
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))

    saved = run("--workspace", "research", "ps")
    refused = run("--workspace", "ops", "ps")
    refused_json = run("--workspace", "ops", "ps", "--format", "json")
    monkeypatch.setenv("LIUM_WORKSPACE", "")  # the root option exported "ops"; the next run asks for nothing
    refused_env = run("rm", "--all", "-y", env={"LIUM_WORKSPACE": "ops"})

    assert saved.exit_code == 0, saved.output
    assert responses.calls[1].request.headers["X-API-KEY"] == "sk_test_research"
    assert "X-Lium-Workspace-Id" not in responses.calls[1].request.headers  # the key selects; no header with a key
    # no key saved for 'ops': nothing runs as another key on another balance, and nothing is sent
    refused_text = " ".join(refused.output.split())
    assert refused.exit_code == EXIT_CONFIGURATION_ERROR
    assert "No API key is saved for workspace 'ops'" in refused_text
    assert "lium keys create <name> --workspace ops --save" in refused_text
    assert refused_json.exit_code == EXIT_CONFIGURATION_ERROR and "'ops'" in refused_json.output
    assert refused_env.exit_code == EXIT_CONFIGURATION_ERROR and "'ops'" in refused_env.output
    assert len(responses.calls) == 2


@responses.activate
def test_a_stored_default_the_key_does_not_act_in_is_a_warning_under_the_table(home):
    write_config(home, "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n[workspaces]\nactive = ops\n")
    me("users_me_personal")
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))

    result = run("ps")
    text = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "Workspace: Ana's workspace (owner) · personal" in text
    assert "the key this command ran with acts in Ana's workspace, not in 'ops'" in text
    assert "lium keys create <name> --workspace ops --save" in text


@responses.activate
def test_an_explicit_workspace_whose_saved_key_acts_elsewhere_stops_up_and_rm(home):
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_stale\n",
    )
    me("users_me_personal")  # the saved key turns out to act in the personal workspace
    me("users_me_personal")
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))

    me("users_me_personal")

    removed = run("--workspace", "research", "rm", "--all", "-y")
    rented = run("--workspace", "research", "up", "some-node-id", "-y", "--no-ssh")
    listed = run("--workspace", "research", "ps")
    text = " ".join(listed.output.split())

    assert removed.exit_code == EXIT_CONFIGURATION_ERROR
    assert "Not acting in 'research'" in " ".join(removed.output.split())
    assert rented.exit_code == EXIT_CONFIGURATION_ERROR and "Not acting in 'research'" in " ".join(rented.output.split())
    # rm and up read /users/me and nothing else (no pod list, no executor, no rent); ps lists, then reads it
    assert [c.request.url.rsplit("/", 1)[-1] for c in responses.calls] == ["me", "me", "pods", "me"]
    assert listed.exit_code == 0 and "the key saved for 'research' acts in Ana's workspace" in text  # a read: warned


@responses.activate
def test_an_explicit_workspace_that_cannot_be_verified_stops_up_and_rm(home):
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research\n",
    )
    responses.add(responses.GET, f"{API}/users/me", status=403, json={"detail": "API key 'ci' belongs to a workspace that is gone"})
    responses.add(responses.GET, f"{API}/users/me", status=403, json={"detail": "API key 'ci' belongs to a workspace that is gone"})

    removed = run("--workspace", "research", "rm", "--all", "-y")
    rented = run("--workspace", "research", "up", "some-node-id", "-y", "--no-ssh")

    for result in (removed, rented):
        text = " ".join(result.output.split())
        assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
        assert "Not acting in 'research'" in text and "could not be checked" in text
    assert [c.request.url.rsplit("/", 1)[-1] for c in responses.calls] == ["me", "me"]  # nothing rented or removed


@responses.activate
def test_a_renamed_workspace_still_matches_its_saved_key_by_id(home):
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research\n",
    )
    renamed = fixture("users_me_research")
    renamed["workspace"]["name"] = "Research Lab"  # an owner renamed it on the server; the saved id is what counts
    responses.add(responses.GET, f"{API}/users/me", json=renamed)
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))
    pod_id = fixture("pods_research")[0]["id"]
    responses.add(responses.DELETE, f"{API}/pods/{pod_id}", json={"message": "deleted"})

    responses.add(responses.GET, f"{API}/users/me", json=renamed)
    responses.add(responses.GET, f"{API}/workspaces", json=[{**fixture("workspaces_key")[0], "name": "Research Lab"}])
    responses.add(responses.GET, f"{API}/workspaces/{RESEARCH}/members", json=fixture("members"))

    result = run("--workspace", "research", "rm", "--all", "-y")
    members = run("workspaces", "members", "research")  # the team commands find it by the saved id too

    assert result.exit_code == 0, result.output
    assert "Workspace: Research Lab (owner)" in result.output and "not in" not in result.output
    assert responses.calls[2].request.method == "DELETE"  # the removal went ahead under the saved key
    assert members.exit_code == 0 and "Research Lab" in members.output and "ben@example.com" in members.output


@responses.activate
def test_delete_after_a_rename_drops_the_section_saved_under_the_old_name(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n[workspaces]\nactive = research\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research\n",
    )
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=[{**fixture("workspaces_key")[0], "name": "Research Lab"}])
    responses.add(responses.DELETE, f"{API}/workspaces/{RESEARCH}", json={"message": "Workspace deleted"})

    result = run("workspaces", "delete", "research", "--yes")

    assert result.exit_code == 0, result.output
    assert "[workspace.research]" not in config_text(home) and "active = research" not in config_text(home)
    assert "sk_test_research" not in config_text(home)  # the dead key is gone with it


@responses.activate
def test_delete_keeps_a_same_named_section_that_holds_another_workspace(home, monkeypatch):
    """Two workspaces named Research: deleting one must not drop the section, key and default saved for the other."""
    other = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n[workspaces]\nactive = Research\n"
        f"[workspace.research]\nid = {other}\napi_key = sk_test_first_research\n",
    )
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.DELETE, f"{API}/workspaces/{RESEARCH}", json={"message": "Workspace deleted"})

    result = run("workspaces", "delete", RESEARCH, "--yes")

    assert result.exit_code == 0, result.output
    assert responses.calls[-1].request.method == "DELETE" and RESEARCH in responses.calls[-1].request.url
    text = config_text(home)
    assert f"[workspace.research]\nid = {other}\napi_key = sk_test_first_research" in text  # the twin's section stays
    assert "active = Research" in text


@responses.activate
def test_workspaces_use_stores_the_default_and_the_key_when_it_acts_there(home):
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))

    result = run("workspaces", "use", "research")

    assert result.exit_code == 0, result.output
    text = config_text(home)
    assert "[workspaces]\nactive = Research" in text
    assert f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_default" in text


@responses.activate
def test_workspaces_use_says_when_a_key_is_already_saved_for_the_default(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")  # a session sees both workspaces; a key sees one
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research\n",
    )
    responses.add(responses.GET, f"{API}/users/me", json=fixture("users_me_personal"))
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))

    result = run("workspaces", "use", "research", env={"LIUM_API_KEY": "sk_test_personal"})
    text = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "already saved and will be used" in text and "No API key is saved" not in text
    assert f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research" in config_text(home)  # untouched


@responses.activate
def test_workspaces_use_saves_the_key_it_runs_with_when_that_key_acts_there(home):
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))
    me("users_me_personal")
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session")[:1])

    saved = run("workspaces", "use", "research", env={"LIUM_API_KEY": "sk_test_research"})
    elsewhere = run("workspaces", "use", "research", env={"LIUM_API_KEY": "sk_test_personal"})

    assert saved.exit_code == 0, saved.output
    assert responses.calls[0].request.headers["X-API-KEY"] == "sk_test_research"  # the env key, not the configured one
    assert f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research" in config_text(home)
    # a key that acts elsewhere sees only its own workspace: the server's answer is that 'research' is not visible
    assert elsewhere.exit_code == EXIT_API_ERROR and "No workspace named 'research'" in elsewhere.output
    assert "sk_test_personal" not in config_text(home)


@responses.activate
def test_the_remedies_for_a_missing_key_work_with_lium_workspace_exported(home, monkeypatch):
    monkeypatch.setenv("LIUM_WORKSPACE", "ops")  # no key saved for it: `ps` is refused, the team commands still run
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.POST, f"{API}/users/login", json=fixture("login"))
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.POST, f"{API}/keys", json=fixture("key_created"))
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))

    refused = run("ps")
    login = run("workspaces", "login", "--email", "ana@example.com", "--password-stdin", input="pw\n")
    created = run("keys", "create", "ci", "--workspace", "Research", "--save")
    used = run("workspaces", "use", "research", env={"LIUM_API_KEY": "sk_test_research"})

    assert refused.exit_code == EXIT_CONFIGURATION_ERROR and "'ops'" in refused.output
    assert login.exit_code == 0, login.output
    assert created.exit_code == 0, created.output
    assert used.exit_code == 0, used.output
    assert responses.calls[0].request.headers["X-API-KEY"] == "sk_test_default"  # the account's key, not one for 'ops'


@responses.activate
def test_workspaces_members_invite_remove_transfer_delete_go_through_the_session(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n[workspaces]\nactive = Research\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research\n",
    )
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.GET, f"{API}/workspaces/{RESEARCH}/members", json=fixture("members"))
    responses.add(responses.POST, f"{API}/workspaces/{RESEARCH}/invitations", json=fixture("invitation"))
    responses.add(responses.DELETE, f"{API}/workspaces/{RESEARCH}/members/{BEN}", json={"message": "Member removed"})
    responses.add(
        responses.POST,
        f"{API}/workspaces/{RESEARCH}/billing-owner/transfer",
        json={**fixture("workspaces_key")[0], "billing_owner_user_id": BEN},
    )
    responses.add(responses.DELETE, f"{API}/workspaces/{RESEARCH}", json={"message": "Workspace deleted"})

    members = run("workspaces", "members", "Research")
    invite = run("workspaces", "invite", "dana@example.com", "Research", "--role", "member")
    remove = run("workspaces", "remove", "ben@example.com", "Research", "--yes")
    # CliRunner swaps sys.stdin for a pipe; tell ui a human can answer the confirm (DAH-2883's is_interactive)
    monkeypatch.setattr("lium.cli.ui.is_interactive", lambda: True)
    declined = run("workspaces", "transfer-billing", BEN, "Research", input="n\n")
    monkeypatch.setattr("lium.cli.ui.is_interactive", lambda: False)
    refused = run("workspaces", "transfer-billing", BEN, "Research", input="n\n")
    transfer = run("workspaces", "transfer-billing", BEN, "Research", "--yes")
    delete = run("workspaces", "delete", "Research", "--yes")

    assert members.exit_code == 0 and "ben@example.com" in members.output and "admin" in members.output
    assert invite.exit_code == 0 and "Invited dana@example.com to Research as member" in invite.output
    sent = next(c for c in responses.calls if c.request.url.endswith("/invitations"))
    assert json.loads(sent.request.body) == {"email": "dana@example.com", "role": "member"}
    assert remove.exit_code == 0 and "Member removed" in remove.output
    assert declined.exit_code == 0 and "Nothing transferred" in declined.output
    refused_text = " ".join(refused.output.split())
    assert refused.exit_code == 2 and "Confirmation required" in refused_text and "--yes" in refused_text  # nothing done
    assert transfer.exit_code == 0 and f"{BEN} now pays for Research" in transfer.output
    assert sum(1 for c in responses.calls if c.request.url.endswith("/billing-owner/transfer")) == 1  # only after yes
    assert delete.exit_code == 0 and "Workspace deleted" in delete.output
    assert "[workspace.research]" not in config_text(home) and "active = Research" not in config_text(home)
    for call in responses.calls:
        if call.request.method != "GET" or call.request.url.endswith("/members"):
            assert call.request.headers.get("Authorization") == "Bearer eyJ.fixture.session"


@responses.activate
def test_the_servers_guard_errors_are_shown_as_they_are(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(
        responses.DELETE,
        f"{API}/workspaces/{RESEARCH}",
        status=409,
        json={"detail": "The workspace still has 1 running pod(s); delete them first"},
    )

    result = run("workspaces", "delete", "Research", "--yes")

    assert result.exit_code == EXIT_API_ERROR
    assert "API error 409: The workspace still has 1 running pod(s)" in result.output


@responses.activate
def test_writes_without_a_session_explain_how_to_get_one_before_any_request(home):
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))

    results = [
        run("workspaces", "invite", "dana@example.com"),
        run("workspaces", "create", "Ops"),
        run("workspaces", "remove", BEN, "--yes"),
        run("workspaces", "transfer-billing", BEN),
        run("workspaces", "delete", "--yes"),
        run("keys", "list"),
        run("keys", "create", "ci"),
    ]

    for result in results:
        assert result.exit_code == EXIT_API_ERROR, result.output
        assert NEEDS_SESSION.split(":")[0] in result.output and "lium workspaces login" in result.output
    assert len(responses.calls) == 0  # not even the capability read went out


@responses.activate
def test_a_missing_session_is_session_required_and_the_hint_names_the_login_not_an_api_key(home):
    """A session refusal is a LiumAuthError too, but its hint must not send anyone to https://lium.io/api-keys."""
    me()

    human = run("keys", "list")
    machine = run("keys", "list", "--json")

    assert human.exit_code == EXIT_API_ERROR and "lium workspaces login" in human.output
    assert "api-keys" not in human.output and "api.api_key" not in human.output
    error = json.loads(machine.stderr)["error"]
    assert error["code"] == "session_required" and error["exit_code"] == EXIT_API_ERROR
    assert "lium workspaces login" in error["hint"] and "api-keys" not in error["hint"]


@responses.activate
def test_an_expired_session_says_to_log_in_again_on_reads_too(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.expired")
    me()
    responses.add(responses.GET, f"{API}/workspaces", status=401, json={"detail": "Invalid or expired token"})

    result = run("workspaces")
    text = " ".join(result.output.split())

    assert result.exit_code == EXIT_API_ERROR
    assert "session token was refused" in text and "run `lium workspaces login` again" in text
    assert "Invalid API key" not in text


@responses.activate
def test_a_refused_session_keeps_the_servers_request_id_but_not_its_key_code_or_hint(home, monkeypatch):
    # the rewrap into LiumSessionError renames the 401 (DAH-3057): the server's request_id survives it, so the
    # refusal can be quoted to support; its API-key code and hint do not — the CLI keeps `session_required` and
    # the login hint, which must never point at an API key
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.expired")
    me()
    responses.add(responses.GET, f"{API}/workspaces", status=401, json={"error": {
        "code": "invalid_api_key", "message": "Invalid or expired token",
        "hint": "Create a key at https://lium.io/settings.", "request_id": "req-401-0007"}})

    result = run("workspaces")
    text = " ".join(result.output.split())

    assert result.exit_code == EXIT_API_ERROR
    assert "run `lium workspaces login` again" in text and "request_id: req-401-0007" in text
    assert "lium.io/settings" not in text

    result = run("workspaces", "list", "--json")
    error = json.loads(result.stderr)["error"]
    assert (error["code"], json.loads(result.stderr)["data"]) == ("session_required", {"request_id": "req-401-0007"})
    assert "lium workspaces login" in error["hint"] and "lium.io/settings" not in error["hint"]


@responses.activate
def test_a_named_workspace_on_a_server_without_them_says_so_without_reading_workspaces(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me("users_me_off")

    members = run("workspaces", "members", "Research")
    keys = run("keys", "list", "--workspace", "Research")

    assert members.exit_code == EXIT_API_ERROR and NOT_ENABLED in members.output
    assert keys.exit_code == EXIT_API_ERROR and NOT_ENABLED in keys.output
    assert [c.request.url.rsplit("/", 1)[-1] for c in responses.calls] == ["me", "me"]


@responses.activate
def test_login_keeps_the_session_token_in_config(home):
    me()
    responses.add(responses.POST, f"{API}/users/login", json=fixture("login"))

    result = run("workspaces", "login", "--email", "ana@example.com", "--password-stdin", input="pw\n")

    assert result.exit_code == 0, result.output
    assert "[session]\ntoken = eyJ.fixture.session" in config_text(home)
    assert json.loads(responses.calls[1].request.body) == {"email": "ana@example.com", "password": "pw"}


@responses.activate
def test_login_without_a_terminal_asks_nothing_and_names_the_flags(home):
    """DAH-2883: a piped `lium workspaces login` never waits on the e-mail or password prompt."""
    me()

    no_email = run("workspaces", "login")
    no_password = run("workspaces", "login", "--email", "ana@example.com")

    no_email_text, no_password_text = " ".join(no_email.output.split()), " ".join(no_password.output.split())
    assert no_email.exit_code == 2 and "Input required: E-mail" in no_email_text and "--email" in no_email_text
    assert no_password.exit_code == 2 and "Input required: Password" in no_password_text
    assert "--password-stdin" in no_password_text
    assert not any(c.request.url.endswith("/users/login") for c in responses.calls)  # nothing was sent


@responses.activate
def test_login_on_a_server_without_workspaces_says_so_and_sends_no_password(home):
    me("users_me_off")

    result = run("workspaces", "login", "--email", "ana@example.com", "--password-stdin", input="pw\n")

    assert result.exit_code == EXIT_API_ERROR and NOT_ENABLED in result.output
    assert not any(c.request.url.endswith("/login") for c in responses.calls)


@responses.activate
def test_a_wrong_password_is_refused_as_a_login_not_as_an_api_key(home):
    me()
    responses.add(responses.POST, f"{API}/users/login", status=401, json={"detail": {"password": "Wrong password."}})

    result = run("workspaces", "login", "--email", "ana@example.com", "--password-stdin", input="nope\n")

    assert result.exit_code == EXIT_API_ERROR
    assert "Login refused: check the e-mail and password" in result.output and "Invalid API key" not in result.output
    assert "[session]" not in config_text(home)


def test_config_get_masks_the_session_token(home):
    write_config(home, "[api]\napi_key = sk_test_default\n[session]\ntoken = eyJ.fixture.session.token.value\n")

    result = run("config", "get", "session.token")

    assert result.exit_code == 0, result.output
    assert "eyJ.fixture.session.token.value" not in result.output and "..." in result.output


@responses.activate
def test_keys_create_binds_the_key_to_the_workspace_and_saves_it_for_the_flag(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.POST, f"{API}/keys", json=fixture("key_created"))

    result = run("keys", "create", "ci", "--workspace", "Research", "--save")

    assert result.exit_code == 0, result.output
    post = responses.calls[-1].request
    assert post.headers["X-Lium-Workspace-Id"] == RESEARCH and json.loads(post.body) == {"name": "ci"}
    assert "sk_test_fixture_key_not_a_secret_0000000000" in result.output
    assert f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_fixture_key_not_a_secret_0000000000" in config_text(home)


@responses.activate
def test_keys_create_save_refuses_a_section_another_same_named_workspace_holds(home, monkeypatch):
    """Two workspaces named Research (one in another team): the second's --save must not overwrite the first's key."""
    other = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {other}\napi_key = sk_test_first_research\n",
    )
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))

    result = run("keys", "create", "ci", "--workspace", RESEARCH, "--save")

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "another workspace named 'Research'" in result.output and other in result.output
    assert not any(c.request.method == "POST" for c in responses.calls)  # refused before a key was minted
    assert f"[workspace.research]\nid = {other}\napi_key = sk_test_first_research" in config_text(home)  # untouched
    # the same id saved again is fine: the section is this workspace's own
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_first_research\n",
    )
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.POST, f"{API}/keys", json=fixture("key_created"))

    again = run("keys", "create", "ci", "--workspace", RESEARCH, "--save")

    assert again.exit_code == 0, again.output
    assert f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_fixture_key_not_a_secret_0000000000" in config_text(home)


@responses.activate
def test_workspaces_use_and_create_refuse_a_section_another_same_named_workspace_holds(home, monkeypatch):
    """`use` would write `active = Research` over a section holding another id, and `Config.load` would then run
    with that other workspace's key; `create --use` is refused before the POST so no workspace is left behind."""
    other = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    before = (
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {other}\napi_key = sk_test_first_research\n"
    )
    write_config(home, before)
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))

    used = run("workspaces", "use", RESEARCH)

    assert used.exit_code == EXIT_CONFIGURATION_ERROR, used.output
    assert "another workspace named 'Research'" in used.output and other in used.output
    assert config_text(home) == before  # neither `active` nor the section was written

    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))

    created = run("workspaces", "create", "research", "--use")

    assert created.exit_code == EXIT_CONFIGURATION_ERROR, created.output
    assert "another workspace named 'research'" in created.output
    assert not any(c.request.method == "POST" for c in responses.calls)  # refused before the workspace exists
    assert config_text(home) == before


@responses.activate
def test_workspace_flag_on_the_team_commands_reads_with_the_saved_key(home):
    write_config(
        home,
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research\n",
    )
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))
    responses.add(responses.GET, f"{API}/workspaces/{RESEARCH}/members", json=fixture("members"))

    result = run("--workspace", "research", "workspaces", "members")

    assert result.exit_code == 0, result.output
    assert "ben@example.com" in result.output
    assert {c.request.headers["X-API-KEY"] for c in responses.calls} == {"sk_test_research"}  # W's key, no session needed


@responses.activate
def test_ls_prints_the_workspace_under_the_table_and_not_in_json(home):
    from test_ls_tier import _make_executor_dict

    responses.add(responses.GET, f"{API}/executors", json=[_make_executor_dict("e1", "secure")])
    me()
    responses.add(responses.GET, f"{API}/executors", json=[_make_executor_dict("e1", "secure")])

    table = run("ls")
    as_json = run("ls", "--format", "json")

    assert table.exit_code == 0, table.output
    assert "Workspace: Research (owner)" in table.output
    assert as_json.exit_code == 0 and json.loads(as_json.output)[0]["id"] == "e1"
    paths = [c.request.url.rsplit("/", 1)[-1].split("?")[0] for c in responses.calls]
    assert paths == ["executors", "me", "executors"]  # the JSON run made no /users/me request


@responses.activate
def test_keys_list_json_leaves_the_key_material_out(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.GET, f"{API}/keys", json=[fixture("key_created")])

    result = run("keys", "list", "--workspace", "Research", "--json")

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows[0]["name"] == "ci" and "key" not in rows[0]
    assert "sk_test_fixture_key_not_a_secret" not in result.output


@responses.activate
def test_a_workspace_name_with_a_percent_sign_round_trips_through_config(home):
    percent = fixture("users_me_research")
    percent["workspace"]["name"] = "100% GPU"
    responses.add(responses.GET, f"{API}/users/me", json=percent)
    responses.add(responses.GET, f"{API}/workspaces", json=[{**fixture("workspaces_key")[0], "name": "100% GPU"}])

    result = run("workspaces", "use", "100% GPU")

    assert result.exit_code == 0, result.output
    assert "active = 100% GPU" in config_text(home) and "[workspace.100% gpu]" in config_text(home)
    loaded = Config.load(workspace="100% GPU")
    assert (loaded.api_key, loaded.workspace_id) == ("sk_test_default", RESEARCH)
    with pytest.raises(ValueError, match="control characters"):
        Config.load(workspace="two\nlines")


@responses.activate
def test_two_workspaces_with_one_name_are_an_error_that_names_both_ids(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    twins = fixture("workspaces_session")
    twins[0] = {**twins[0], "name": "Research"}  # the personal one renamed to the team's name
    responses.add(responses.GET, f"{API}/workspaces", json=twins)

    result = run("workspaces", "delete", "Research", "--yes")
    text = " ".join(result.output.split())

    assert result.exit_code == EXIT_API_ERROR
    assert "2 workspaces are named 'Research'" in text and PERSONAL in text and RESEARCH in text and "use the id" in text
    assert not any(c.request.method == "DELETE" for c in responses.calls)


@responses.activate
def test_unknown_member_or_workspace_is_a_configuration_error(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.GET, f"{API}/workspaces/{RESEARCH}/members", json=fixture("members"))
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))

    member = run("workspaces", "remove", "nobody@example.com", "Research", "--yes")
    workspace = run("workspaces", "members", "Nowhere")

    assert member.exit_code == EXIT_CONFIGURATION_ERROR and "No member 'nobody@example.com'" in member.output
    assert workspace.exit_code == EXIT_API_ERROR and "No workspace named 'Nowhere'" in workspace.output
