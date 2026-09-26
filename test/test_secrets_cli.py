"""`lium secrets` and `Lium.secrets` — a value only ever travels on stdin or a hidden prompt and
through `encrypt_for_upload`; listings carry names and times, never values; with LIUM_SECRETS_ENABLED
unset the CLI and the rent payloads are exactly today's."""

import json
from types import SimpleNamespace

import pytest
import responses
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.secrets import command as secrets_command
from lium.cli.up import command as up_command
from lium.sdk import Config, Lium, LiumError
from lium.sdk import secrets as sdk_secrets
from lium.sdk.secrets import SecretInfo, encrypt_for_upload

BASE = "https://lium.io/api"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIUserKeyForTestingPurposesOnly user@test"
VALUE = "hf_SECRET_VALUE_MARKER"


class FakeSecrets:
    def __init__(self):
        self.set_calls = []
        self.deleted = []
        self.rows = [SecretInfo(name="HF_TOKEN", updated_at="2026-09-23T12:00:00Z")]

    def list(self):
        return list(self.rows)

    def set(self, name, value):
        self.set_calls.append((name, value))
        return SecretInfo(name=name)

    def delete(self, name):
        self.deleted.append(name)


@pytest.fixture
def fake(monkeypatch):
    secrets = FakeSecrets()
    monkeypatch.setattr(secrets_command, "Lium", lambda *a, **k: SimpleNamespace(secrets=secrets))
    monkeypatch.setattr(secrets_command, "ensure_config", lambda: None)
    monkeypatch.setenv("LIUM_SECRETS_ENABLED", "1")
    return secrets


def _run(args, **kwargs):
    return CliRunner().invoke(cli, args, catch_exceptions=False, **kwargs)


# --- CLI -------------------------------------------------------------------------------------


def test_set_takes_no_value_parameter_at_all():
    assert [p.name for p in secrets_command.secrets_set_command.params] == ["name"]


def test_set_reads_the_value_from_stdin(fake):
    result = _run(["secrets", "set", "HF_TOKEN"], input=f"{VALUE}\n")

    assert result.exit_code == 0, result.output
    assert fake.set_calls == [("HF_TOKEN", VALUE)]
    assert VALUE not in result.output


def test_set_keeps_inner_newlines_and_drops_only_the_last(fake):
    _run(["secrets", "set", "PEM"], input="line1\nline2\n")
    assert fake.set_calls == [("PEM", "line1\nline2")]


def test_set_prompts_hidden_and_twice_when_stdin_is_a_terminal(fake, monkeypatch):
    monkeypatch.setattr(secrets_command, "stdin_is_interactive", lambda: True)

    result = _run(["secrets", "set", "HF_TOKEN"], input=f"{VALUE}\n{VALUE}\n")

    assert result.exit_code == 0, result.output
    assert fake.set_calls == [("HF_TOKEN", VALUE)]
    assert "Value for HF_TOKEN" in result.output
    assert VALUE not in result.output


def test_a_value_on_argv_is_refused_without_echoing_it(fake):
    result = CliRunner().invoke(cli, ["secrets", "set", "HF_TOKEN", VALUE])

    assert result.exit_code == 2
    assert fake.set_calls == []
    assert "never taken from the command line" in result.output
    assert VALUE not in result.output


def test_an_empty_value_is_refused(fake):
    result = CliRunner().invoke(cli, ["secrets", "set", "HF_TOKEN"], input="\n")
    assert result.exit_code == 2
    assert fake.set_calls == []


def test_a_bad_name_is_refused_before_reading_anything(fake):
    result = CliRunner().invoke(cli, ["secrets", "set", "../etc/passwd"], input=VALUE)
    assert result.exit_code == 2
    assert fake.set_calls == []


def test_list_shows_names_and_times_never_values(fake):
    fake.rows = [SecretInfo(name="HF_TOKEN", updated_at="2026-09-23T12:00:00Z"), SecretInfo(name="WANDB_API_KEY")]

    table = _run(["secrets", "list"])
    as_json = _run(["secrets", "list", "--json"])

    assert "HF_TOKEN" in table.output and "WANDB_API_KEY" in table.output
    assert json.loads(as_json.output) == [
        {"name": "HF_TOKEN", "updated_at": "2026-09-23T12:00:00Z"},
        {"name": "WANDB_API_KEY", "updated_at": None},
    ]


def test_rm_deletes_after_yes(fake):
    result = _run(["secrets", "rm", "HF_TOKEN", "-y"])
    assert result.exit_code == 0, result.output
    assert fake.deleted == ["HF_TOKEN"]


@pytest.mark.parametrize("args", [["secrets", "list"], ["secrets", "rm", "HF_TOKEN", "-y"], ["secrets", "set", "HF_TOKEN"]])
def test_flag_off_refuses_every_subcommand(fake, monkeypatch, args):
    monkeypatch.delenv("LIUM_SECRETS_ENABLED")

    result = CliRunner().invoke(cli, args, input=VALUE)

    assert result.exit_code == 2
    assert "LIUM_SECRETS_ENABLED=1" in result.output
    assert fake.set_calls == [] and fake.deleted == []


def test_flag_off_hides_the_group_and_up_secret():
    # hidden is fixed at import, and the suite runs with the flag unset
    assert secrets_command.secrets_command.hidden is True
    assert next(p for p in up_command.up_command.params if p.name == "secret_names").hidden is True
    assert "secrets" not in _run(["--help"]).output
    assert "--secret" not in _run(["up", "--help"]).output


def test_up_secret_with_the_flag_off_is_refused(monkeypatch):
    monkeypatch.delenv("LIUM_SECRETS_ENABLED", raising=False)
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["up", "exec-1", "--secret", "HF_TOKEN", "-y"])

    assert result.exit_code == 2
    assert "LIUM_SECRETS_ENABLED=1" in result.output


# --- SDK -------------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LIUM_SECRETS_ENABLED", "1")
    monkeypatch.setattr(Lium, "_ensure_ssh_keys_registered", lambda self, keys, name=None: None)
    lium = Lium(Config(api_key="test"))
    monkeypatch.setattr(lium, "get_executor", lambda executor_id: SimpleNamespace(id="exec-1"))
    monkeypatch.setattr(lium, "_pod_ids_before_rent", lambda: set())
    return lium


@responses.activate
def test_sdk_set_sends_the_body_encrypt_for_upload_returns(client, monkeypatch):
    seen = []

    def fake_encrypt(name, value):
        seen.append((name, value))
        return {"ciphertext": "opaque", "encryption": "test-scheme"}

    monkeypatch.setattr(sdk_secrets, "encrypt_for_upload", fake_encrypt)
    responses.add(responses.PUT, f"{BASE}/secrets/HF_TOKEN", json={"name": "HF_TOKEN", "updated_at": "t"})

    info = client.secrets.set("HF_TOKEN", VALUE)

    assert seen == [("HF_TOKEN", VALUE)]
    assert json.loads(responses.calls[0].request.body) == {"ciphertext": "opaque", "encryption": "test-scheme"}
    assert info == SecretInfo(name="HF_TOKEN", updated_at="t")


def test_encrypt_for_upload_is_todays_no_op():
    assert encrypt_for_upload("HF_TOKEN", VALUE) == {"value": VALUE, "encryption": "none"}


@responses.activate
def test_sdk_list_keeps_names_and_times_even_if_a_server_sent_a_value(client):
    responses.add(responses.GET, f"{BASE}/secrets", json=[{"name": "HF_TOKEN", "updated_at": "t", "value": VALUE}])

    rows = client.secrets.list()

    assert rows == [SecretInfo(name="HF_TOKEN", updated_at="t")]
    assert VALUE not in repr(rows)


@responses.activate
def test_sdk_delete(client):
    responses.add(responses.DELETE, f"{BASE}/secrets/HF_TOKEN", status=204)
    client.secrets.delete("HF_TOKEN")
    assert responses.calls[0].request.method == "DELETE"


def test_sdk_refuses_with_the_flag_off(client, monkeypatch):
    monkeypatch.delenv("LIUM_SECRETS_ENABLED")
    with pytest.raises(LiumError, match="LIUM_SECRETS_ENABLED=1"):
        client.secrets.list()
    with pytest.raises(LiumError, match="LIUM_SECRETS_ENABLED=1"):
        client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=["HF_TOKEN"])


@responses.activate
def test_up_sends_secret_names_and_never_a_value(client):
    responses.add(responses.POST, f"{BASE}/executors/exec-1/rent", json={"id": "pod-1"})

    client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=["HF_TOKEN", "WANDB", "HF_TOKEN"])

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["secret_names"] == ["HF_TOKEN", "WANDB"]


@responses.activate
def test_up_without_secrets_sends_todays_payload(client):
    responses.add(responses.POST, f"{BASE}/executors/exec-1/rent", json={"id": "pod-1"})

    client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY])

    assert json.loads(responses.calls[-1].request.body) == {
        "pod_name": "Your Pod", "template_id": "tpl", "dockerfile_content": None, "volume_id": None,
        "user_public_key": [KEY], "initial_port_count": None, "enable_volume_encryption": True,
        "backup_log_id": None, "restore_path": None,
    }


def test_up_refuses_a_bad_secret_name_before_renting(client):
    with pytest.raises(ValueError):
        client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=["BAD-NAME"])


# --- a rejected name is never echoed: it may be a pasted `NAME=value` ------------------------------

PASTED = f"HF_TOKEN={VALUE}"


def _everything_printed(result):
    try:
        return result.stdout + result.stderr
    except ValueError:  # click < 8.2 without mix_stderr=False: stderr is already in output
        return result.output


@pytest.fixture
def up_ready(monkeypatch):
    monkeypatch.setenv("LIUM_SECRETS_ENABLED", "1")
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)


@pytest.mark.parametrize("args", [
    ["secrets", "set", PASTED],
    ["secrets", "rm", PASTED, "-y"],
    ["up", "exec-1", "--secret", PASTED, "-y"],
    ["up", "exec-1", "--secret", "HF_TOKEN", "--secret", PASTED, "-y"],
])
@pytest.mark.parametrize("json_mode", [False, True])
def test_a_pasted_name_value_is_refused_without_echoing_the_value(fake, up_ready, monkeypatch, args, json_mode):
    if json_mode:
        monkeypatch.setenv("LIUM_OUTPUT", "json")

    result = CliRunner().invoke(cli, args, input="")
    printed = _everything_printed(result)

    assert result.exit_code == 2
    assert fake.set_calls == [] and fake.deleted == []
    assert VALUE not in printed
    assert "can't contain '='" in printed and "HF_TOKEN" not in printed
    if json_mode:
        assert json.loads(printed.strip().splitlines()[-1])["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("args", [
    ["secrets", "rm", "HF_TOKEN", VALUE, "-y"],
    ["secrets", "list", VALUE],
    ["secrets", PASTED],
    ["secrets", "set", "HF_TOKEN", f"--value={VALUE}"],
])
def test_stray_arguments_are_refused_without_echoing_them(fake, args):
    result = CliRunner().invoke(cli, args, input="")

    assert result.exit_code == 2
    assert fake.set_calls == [] and fake.deleted == []
    assert VALUE not in _everything_printed(result)


@pytest.mark.parametrize("name", [PASTED, f"=={VALUE}", f"bad-{VALUE}", f"{VALUE}-x=y"])
def test_sdk_refusal_never_contains_the_rejected_name(client, name):
    for call in (lambda: client.secrets.set(name, "v"), lambda: client.secrets.delete(name),
                 lambda: client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=[name])):
        with pytest.raises(ValueError) as caught:
            call()
        assert VALUE not in str(caught.value) and VALUE not in repr(caught.value)


def test_the_refusal_repeats_no_part_of_the_name():
    assert sdk_secrets.invalid_secret_name_message(f"HF_TOKEN={VALUE}") == sdk_secrets.invalid_secret_name_message("x==")
    assert sdk_secrets.invalid_secret_name_message(f"bad-{VALUE}") == sdk_secrets.invalid_secret_name_message("-")


# A value pasted as the name, in the shapes that fooled a prefix check: base64 padding leaves a
# name-shaped part before the first '='. Every marker below must be absent from all output.
TOKEN = "Zm9vYmFyQmF6UXV4X2xvbmdfcmFuZG9tX3Rva2VuXzEyMzQ1Njc4OTA+c2VjcmV0/dmFsdWU"
PADDED_SHAPES = {
    "value==": (f"{VALUE}==", VALUE),
    "value=": (f"{VALUE}=", VALUE),
    "==": ("==", None),
    "NAME=": (f"{VALUE}_NAME=", VALUE),
    "token": (TOKEN, TOKEN),
    "token==": (f"{TOKEN}==", TOKEN),
    "HF=token=": (f"HF={TOKEN}=", TOKEN),
}
TOKEN_PARTS = [p for p in TOKEN.replace("+", "/").split("/") if len(p) >= 8]


def _assert_nothing_leaks(text, marker):
    if marker:
        assert marker not in text
        if marker == TOKEN:
            assert not any(part in text for part in TOKEN_PARTS)


@pytest.mark.parametrize("shape", PADDED_SHAPES, ids=list(PADDED_SHAPES))
@pytest.mark.parametrize("command", ["set", "rm", "up", "up-second"])
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_padded_or_bare_values_as_names_never_leak_from_the_cli(fake, up_ready, monkeypatch, shape, command, json_mode):
    name, marker = PADDED_SHAPES[shape]
    args = {
        "set": ["secrets", "set", name],
        "rm": ["secrets", "rm", name, "-y"],
        "up": ["up", "exec-1", "--secret", name, "-y"],
        "up-second": ["up", "exec-1", "--secret", "HF_TOKEN", "--secret", name, "-y"],
    }[command]
    if json_mode:
        monkeypatch.setenv("LIUM_OUTPUT", "json")

    result = CliRunner().invoke(cli, args, input="")
    printed = _everything_printed(result)

    assert result.exit_code == 2, printed
    assert fake.set_calls == [] and fake.deleted == []
    _assert_nothing_leaks(printed, marker)
    if json_mode:
        assert json.loads(printed.strip().splitlines()[-1])["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("shape", PADDED_SHAPES, ids=list(PADDED_SHAPES))
def test_padded_or_bare_values_as_names_never_leak_from_the_sdk(client, shape):
    name, marker = PADDED_SHAPES[shape]
    calls = {
        "set": lambda: client.secrets.set(name, "v"),
        "delete": lambda: client.secrets.delete(name),
        "up": lambda: client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=[name]),
        "rent": lambda: client.rent(gpu_type="H100", ssh_keys=[KEY], secret_names=["HF_TOKEN", name]),
    }
    for label, call in calls.items():
        with pytest.raises(ValueError) as caught:
            call()
        _assert_nothing_leaks(str(caught.value) + repr(caught.value), marker)


def test_bare_secrets_still_lists(fake):
    result = _run(["secrets"])
    assert result.exit_code == 0, result.output
    assert "HF_TOKEN" in result.output


# --- secret_names on Lium.rent and the `lium up` rent hand-off ---------------------------------

def _rent_node(node_id="exec-1"):
    return {
        "id": node_id, "machine_name": "NVIDIA H100 NVL", "price_per_gpu": 1.2, "gpu_count": 1,
        "available_gpu_count": 1, "location": {"country": "DE", "country_code": "DE"},
        "effective_download_speed_mbps": 900.0, "effective_upload_speed_mbps": 500.0,
        "specs": {"gpu": {"count": 1, "details": [{"name": "NVIDIA H100 NVL", "capacity": 81559}]},
                  "cpu": {"count": 32}, "available_port_count": 10, "sysbox_runtime": False},
    }


def _rent_backend(server_side):
    body = {"started_at": "2026-09-06T00:00:00+00:00", "uptime_seconds": 1}
    if server_side:
        body["features"] = ["rent_by_spec"]
    responses.add(responses.GET, f"{BASE}/version", json=body)
    responses.add(responses.GET, f"{BASE}/pods", json=[])
    responses.add(responses.GET, f"{BASE}/machines", json=[{"name": "NVIDIA H100 NVL"}])
    responses.add(responses.GET, f"{BASE}/executors", json=[_rent_node()])
    responses.add(responses.POST, f"{BASE}/executors/rent-by-spec", json={
        "success": True, "dry_run": False, "pod_id": "pod-1", "template_id": "tpl", "price_per_hour": 1.2,
        "selected_executor": _rent_node(), "candidates": 1, "attempts": 1,
    })
    responses.add(responses.POST, f"{BASE}/executors/exec-1/rent", json={"id": "pod-1"})


def _rent_posts():
    return [json.loads(c.request.body) for c in responses.calls
            if c.request.method == "POST" and "/rent" in c.request.url]


@responses.activate
@pytest.mark.parametrize("server_side", [True, False], ids=["rent-by-spec", "client-side"])
def test_sdk_rent_sends_secret_names_and_never_a_value(client, server_side):
    _rent_backend(server_side)

    client.rent(gpu_type="H100", name="p", template_id="tpl", ssh_keys=[KEY], secret_names=["HF_TOKEN", "WANDB", "HF_TOKEN"])

    (sent,) = _rent_posts()
    assert sent["secret_names"] == ["HF_TOKEN", "WANDB"]
    assert VALUE not in json.dumps(sent)


@responses.activate
@pytest.mark.parametrize("server_side", [True, False], ids=["rent-by-spec", "client-side"])
def test_sdk_rent_without_secrets_sends_no_secret_key(client, server_side):
    _rent_backend(server_side)

    client.rent(gpu_type="H100", name="p", template_id="tpl", ssh_keys=[KEY])

    (sent,) = _rent_posts()
    assert "secret_names" not in sent


@responses.activate
def test_sdk_rent_with_the_flag_off_refuses_before_any_request(client, monkeypatch):
    monkeypatch.delenv("LIUM_SECRETS_ENABLED")

    with pytest.raises(LiumError, match="LIUM_SECRETS_ENABLED=1"):
        client.rent(gpu_type="H100", ssh_keys=[KEY], secret_names=["HF_TOKEN"])
    assert len(responses.calls) == 0


class RecordingLium:
    def __init__(self):
        self.calls = []

    def rent(self, **kwargs):
        self.calls.append(("rent", kwargs))
        return SimpleNamespace(pod={"id": "pod-1"}, executor=kwargs, price_per_hour=1.0, gpu_count=1)

    def up(self, **kwargs):
        self.calls.append(("up", kwargs))
        return {"id": "pod-1"}


@pytest.mark.parametrize("spec", [{"gpu_type": "H100", "gpu_count": 1}, None], ids=["rent-by-spec", "node"])
@pytest.mark.parametrize("secret_names", [["HF_TOKEN"], []], ids=["secret", "no-secret"])
def test_rent_action_hands_over_names_only_and_nothing_when_unset(spec, secret_names):
    from lium.cli.up.actions import RentPodAction

    lium = RecordingLium()
    executor = SimpleNamespace(id="exec-1", price_per_gpu=1.0, price_per_hour=1.0, gpu_count=1)
    result = RentPodAction().execute({
        "lium": lium, "executor": executor, "spec": spec, "template": SimpleNamespace(id="tpl"),
        "name": "p", "secret_names": secret_names,
    })

    assert result.ok
    ((method, kwargs),) = lium.calls
    assert method == ("rent" if spec else "up")
    if secret_names:
        assert kwargs["secret_names"] == ["HF_TOKEN"]
    else:
        assert "secret_names" not in kwargs


def test_lium_up_secret_reaches_the_rent_as_a_name(monkeypatch):
    from lium.sdk import ExecutorInfo, RentResult

    node = ExecutorInfo(
        id="id-thrifty-node-bb", huid="thrifty-node-bb", machine_name="NVIDIA GeForce RTX 4090", gpu_type="RTX4090",
        gpu_count=1, price_per_hour=0.30, price_per_gpu=0.30, location={"country": "Germany", "country_code": "DE"},
        specs={}, status="available", docker_in_docker=False, ip="1.2.3.4", effective_download_speed_mbps=900.0,
    )

    class Ready:
        workspaces = SimpleNamespace(current=lambda: None)

        def __init__(self):
            self.rents = []

        def supports(self, feature):
            return feature == "rent_by_spec"

        def rent(self, **kwargs):
            self.rents.append(kwargs)
            pod = None if kwargs.get("dry_run") else {"id": "pod-uuid-1", "name": kwargs["name"]}
            return RentResult(executor=node, price_per_hour=0.30, template_id="tpl-default", candidates=1,
                              pod=pod, attempts=1, dry_run=bool(kwargs.get("dry_run")), server_side=True)

        def get_template(self, template_id):
            return SimpleNamespace(id=template_id, name="pytorch")

        def get_deployment_estimate(self, executor_id, template_id):
            return {}

        def ps(self):
            return [SimpleNamespace(id="pod-uuid-1", huid="thrifty-node-bb", name="thrifty-node-bb", gpu_count=1,
                                    status="RUNNING", ssh_cmd="ssh root@pod.example", ports={"22": 10022})]

        def wait_ready(self, pod, *, timeout=None, poll_interval=None, on_poll=None):
            return self.ps()[0]

    made = []
    monkeypatch.setattr(up_command, "Lium", lambda *a, **k: made.append(Ready()) or made[-1])
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setenv("LIUM_SECRETS_ENABLED", "1")

    with_secret = CliRunner().invoke(cli, ["up", "--gpu", "RTX4090", "-y", "--no-ssh", "--secret", "HF_TOKEN"])
    without = CliRunner().invoke(cli, ["up", "--gpu", "RTX4090", "-y", "--no-ssh"])

    assert with_secret.exit_code == 0 and without.exit_code == 0, with_secret.output + without.output
    rent_with = [r for r in made[0].rents if not r.get("dry_run")]
    rent_without = [r for r in made[1].rents if not r.get("dry_run")]
    assert [r["secret_names"] for r in rent_with] == [["HF_TOKEN"]]
    assert all("secret_names" not in r for r in rent_without)
    assert {k: v for k, v in rent_with[0].items() if k != "secret_names"} == rent_without[0]


# --- click's own parse errors quote the offending token; under secrets they must not ---------------

class NoNodes:
    workspaces = SimpleNamespace(current=lambda: None)

    def __init__(self, *args, **kwargs):
        pass

    def supports(self, feature):
        return False

    def get_executor(self, executor_id):
        return None


UNPARSEABLE = {
    "unknown option": ["secrets", f"--tok{VALUE}"],
    "unknown option after --": ["secrets", "--", f"--tok{VALUE}"],
    "unknown option after --help": ["secrets", "--help", f"--tok{VALUE}"],
    "unknown subcommand": ["secrets", VALUE],
    "set extra after --": ["secrets", "set", "HF_TOKEN", "--", VALUE],
    "set unknown option": ["secrets", "set", "HF_TOKEN", f"--tok{VALUE}"],
    "rm extra after --": ["secrets", "rm", "-y", "HF_TOKEN", "--", f"--tok{VALUE}"],
    "list extra after --": ["secrets", "list", "--", VALUE],
    "up --secret unknown option": ["up", "--secret", "HF_TOKEN", f"--tok{VALUE}"],
    "up --secret extra argument": ["up", "exec-1", "--secret", "HF_TOKEN", VALUE, "-y"],
    "up --secret value as NODE_ID": ["up", "--secret", "HF_TOKEN", VALUE, "-y"],
    "up --secret value as NODE_ID after --": ["up", "--secret", "HF_TOKEN", "-y", "--", f"--tok{VALUE}"],
}


@pytest.mark.parametrize("case", UNPARSEABLE, ids=list(UNPARSEABLE))
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_unparseable_input_is_refused_without_repeating_it(fake, up_ready, monkeypatch, case, json_mode):
    monkeypatch.setattr(up_command, "Lium", NoNodes)
    if json_mode:
        monkeypatch.setenv("LIUM_OUTPUT", "json")

    result = CliRunner().invoke(cli, UNPARSEABLE[case], input="")
    printed = _everything_printed(result)

    assert result.exit_code != 0, printed
    assert fake.set_calls == [] and fake.deleted == []
    assert VALUE not in printed


def test_up_without_secret_keeps_clicks_own_message(monkeypatch):
    result = CliRunner().invoke(cli, ["up", "--gpus", "x"])
    assert result.exit_code == 2
    assert "No such option '--gpus'" in result.output


def test_up_without_secret_still_names_a_missing_node(up_ready, monkeypatch):
    monkeypatch.setattr(up_command, "Lium", NoNodes)
    result = CliRunner().invoke(cli, ["up", "some-node", "-y"])
    assert "some-node" in result.output


# --- Lium.rental forwards secret_names like up/rent ---------------------------------------------

@responses.activate
def test_rental_sends_secret_names(client, monkeypatch):
    responses.add(responses.POST, f"{BASE}/executors/exec-1/rent", json={"id": "pod-1"})
    monkeypatch.setattr(client, "_pod_ids_or_none", lambda: frozenset())
    monkeypatch.setattr(client, "wait_ready", lambda pod, timeout=None: SimpleNamespace(id="pod-1"))
    removed = []
    monkeypatch.setattr(client, "_remove_quietly", removed.append)

    with client.rental(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=["HF_TOKEN", "HF_TOKEN"]):
        pass

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["secret_names"] == ["HF_TOKEN"]
    assert removed == [{"id": "pod-1"}]


def test_rental_refuses_a_bad_secret_name_without_repeating_it(client, monkeypatch):
    monkeypatch.setattr(client, "_pod_ids_or_none", lambda: None)
    with pytest.raises(ValueError) as caught:
        with client.rental(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=[f"{VALUE}=="]):
            pass
    assert VALUE not in str(caught.value)


# --- a credential pasted as the name is refused, never sent, never shown ---------------------------
# Built at run time so no token-shaped literal sits in the repo (secret scanners would flag it).
_BODY = "".join("aB3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zC"[(i * 7) % 32] for i in range(40))
CREDENTIALS = {
    "hugging face": "hf" + "_" + _BODY[:34],
    "github classic": "gh" + "p_" + _BODY[:36],
    "github oauth": "gh" + "o_" + _BODY[:36],
    "github app": "gh" + "s_" + _BODY[:36],
    "github user-to-server": "gh" + "u_" + _BODY[:36],
    "github fine-grained": "github" + "_pat_" + "11A" + _BODY[:19] + "_" + _BODY[3:40],
    "slack bot": "xox" + "b_" + "123456789012_" + _BODY[:24],
    "slack user": "XOX" + "P_" + _BODY[:30],
    "stripe live": "sk" + "_live_" + _BODY[:24],
    "stripe publishable": "pk" + "_live_" + _BODY[:24],
    "openai underscore": "sk" + "_" + _BODY[:32],
    "aws access key id": "AK" + "IA" + "IOSFODNN7EXAMPL3",
    "aws session key id": "AS" + "IA" + "QWERTYUIOP123456",
    "gitlab": "gl" + "pat_" + _BODY[:20],
    "npm": "np" + "m_" + _BODY[:36],
    "long random run": "Q" + _BODY[:30],
    "hex digest": "a3f9c2e1b4d5a6f7e8d9c0b1a2f3e4d5c6b7a8f9",
    "over 64 characters": "A" * 65,
}
ORDINARY_NAMES = [
    "HF_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "HF_HUB_ACCESS_TOKEN", "GITHUB_PAT", "GH_TOKEN_V2",
    "SK_LIVE_KEY", "NPM_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "WANDB_API_KEY",
    "ANTHROPIC_API_KEY", "DATABASE_URL_2024", "ASIA_REGION", "hf_token", "sk_prod_key", "PEM", "_PRIVATE",
    "A" * 64,
]


@pytest.mark.parametrize("name", ORDINARY_NAMES)
def test_ordinary_names_are_accepted(name):
    assert sdk_secrets.validate_secret_name(name) == name


@pytest.mark.parametrize("shape", CREDENTIALS, ids=list(CREDENTIALS))
def test_credential_shapes_are_refused_by_the_validator(shape):
    with pytest.raises(ValueError) as caught:
        sdk_secrets.validate_secret_name(CREDENTIALS[shape])
    assert CREDENTIALS[shape] not in str(caught.value)
    assert "looks like a secret value" in str(caught.value)


@pytest.fixture
def real_client_cli(monkeypatch):
    """The CLI against the real SDK with an HTTP layer that has no routes: any request fails the test."""
    monkeypatch.setenv("LIUM_SECRETS_ENABLED", "1")
    monkeypatch.setenv("LIUM_API_KEY", "test")
    monkeypatch.setattr(secrets_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.mark.parametrize("shape", CREDENTIALS, ids=list(CREDENTIALS))
@pytest.mark.parametrize("command", ["set", "rm", "up", "up-second"])
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_credential_as_name_is_refused_by_the_cli_before_any_request(real_client_cli, monkeypatch, shape, command, json_mode):
    token = CREDENTIALS[shape]
    args = {
        "set": ["secrets", "set", token],
        "rm": ["secrets", "rm", token, "-y"],
        "up": ["up", "exec-1", "--secret", token, "-y"],
        "up-second": ["up", "exec-1", "--secret", "HF_TOKEN", "--secret", token, "-y"],
    }[command]
    if json_mode:
        monkeypatch.setenv("LIUM_OUTPUT", "json")

    result = CliRunner().invoke(cli, args, input=f"{VALUE}\n")
    printed = _everything_printed(result)

    assert result.exit_code == 2, printed
    assert token not in printed
    assert "looks like a secret value" in " ".join(printed.split())
    assert len(real_client_cli.calls) == 0
    if json_mode:
        assert json.loads(printed.strip().splitlines()[-1])["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("shape", CREDENTIALS, ids=list(CREDENTIALS))
def test_credential_as_name_is_refused_by_the_sdk_before_any_request(client, shape):
    token = CREDENTIALS[shape]
    calls = {
        "set": lambda: client.secrets.set(token, VALUE),
        "delete": lambda: client.secrets.delete(token),
        "up": lambda: client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=[token]),
        "rent": lambda: client.rent(gpu_type="H100", ssh_keys=[KEY], secret_names=["HF_TOKEN", token]),
        "rental": lambda: client.rental(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=[token]).__enter__(),
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        for label, call in calls.items():
            with pytest.raises(ValueError) as caught:
                call()
            assert token not in str(caught.value) + repr(caught.value), label
        assert len(mock.calls) == 0


def test_an_ordinary_name_still_goes_through_the_cli(real_client_cli):
    real_client_cli.add(responses.PUT, f"{BASE}/secrets/HF_TOKEN", json={"name": "HF_TOKEN", "updated_at": "t"})

    result = CliRunner().invoke(cli, ["secrets", "set", "HF_TOKEN"], input=f"{VALUE}\n")

    assert result.exit_code == 0, result.output
    assert [c.request.url for c in real_client_cli.calls] == [f"{BASE}/secrets/HF_TOKEN"]
