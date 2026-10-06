"""DAH-3075: `lium mine` renders the executor .env a provider can actually be reached on.

A blank "Public SSH port" answer used to keep the template's hard-coded 2200 while SSH_PORT took
the provider's value, so the validator was told to SSH to a port nothing listens on; and a failed
`docker compose up` showed the first 4,000 characters of stderr (pull progress), never the error.
"""

import sys

import pytest

from lium.cli.commands import mine

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
TEMPLATE = """# executor settings
MINER_HOTKEY_SS58_ADDRESS=
INTERNAL_PORT=8080
EXTERNAL_PORT=8080
SSH_PORT=2200
SSH_PUBLIC_PORT=2200 # Optional. in case you are using proxy and public port is different from internal port in your server
RENTING_PORT_RANGE=
"""


def _env(executor_dir) -> dict[str, str]:
    lines = (executor_dir / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#") and "=" in line)


@pytest.fixture
def executor_dir(tmp_path):
    (tmp_path / ".env.template").write_text(TEMPLATE)
    return tmp_path


def test_blank_public_port_follows_ssh_port(executor_dir):
    mine._setup_executor_env(executor_dir, hotkey=HOTKEY, ssh_port=30311, ssh_public_port="")

    env = _env(executor_dir)
    assert env["SSH_PORT"] == "30311"
    assert env["SSH_PUBLIC_PORT"] == "30311"  # not the template's 2200


def test_explicit_public_port_is_kept(executor_dir):
    mine._setup_executor_env(executor_dir, hotkey=HOTKEY, ssh_port=30311, ssh_public_port="40311")

    env = _env(executor_dir)
    assert env["SSH_PORT"] == "30311"
    assert env["SSH_PUBLIC_PORT"] == "40311"


def test_env_overrides_replace_a_stale_public_port(executor_dir):
    (executor_dir / ".env").write_text("SSH_PORT=2200\nSSH_PUBLIC_PORT=2200\n")

    mine._apply_env_overrides(executor_dir, internal="8080", external="8080", ssh="30311", ssh_pub="", rng="")

    env = _env(executor_dir)
    assert env["SSH_PORT"] == "30311"
    assert env["SSH_PUBLIC_PORT"] == "30311"


def test_run_failure_keeps_the_last_lines_of_stderr():
    noise = "Pulling fs layer\\n" * 400  # ~6,000 chars, like compose's image-pull progress
    script = f'import sys; sys.stderr.write("{noise}"); sys.stderr.write("Error response from daemon: error mounting /root/.bittensor/wallets"); sys.exit(1)'

    with pytest.raises(RuntimeError) as exc_info:
        mine._run([sys.executable, "-c", repr(script)])

    message = str(exc_info.value)
    assert message.startswith("Command failed (1)")
    # the command line is echoed first and contains the script itself — look in the stderr section only
    stderr_section = message.split("--- stderr ---", 1)[1]
    assert stderr_section.rstrip().endswith("Error response from daemon: error mounting /root/.bittensor/wallets")
