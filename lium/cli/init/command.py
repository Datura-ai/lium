"""Init command implementation."""

import json
import os

import click
from rich.markup import escape

from lium.cli import ui
from lium.cli.interactive import is_interactive
from lium.cli.settings import config
from lium.cli.utils import CliFailure, EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR, handle_errors
from lium.sdk.config import API_KEY_ENV_VAR, API_KEY_SECTION_ENV_VAR, Config
from .actions import (
    SaveApiKeyAction,
    SetupApiKeyAction,
    RequestAuthUrlAction,
    VerifySessionAction,
    SetupSshKeyAction,
)

KEYS_URL = "https://lium.io/api-keys"
HEADLESS_HINT = (
    f"No browser? Pass the key: 'lium init --api-key <key>' (create one at {KEYS_URL}), "
    "or export LIUM_API_KEY and skip init."
)
# What to do next when the key passed with --api-key is not saved; nothing was written, so the
# generic hints ("'lium config get api.api_key' shows which one is used") would point at nothing.
_FLAG_KEY_HINTS = {
    "invalid_api_key": "The key passed with --api-key was refused and nothing was saved; "
                       f"create one at {KEYS_URL} and pass it again",
    "api_unreachable": "Nothing was saved; check the network (or LIUM_BASE_URL) and run the same command again",
    "empty_api_key": "Nothing was saved; --api-key needs the key itself (an unset shell variable expands to nothing)",
}


@click.command("init")
@click.option("--api-key", "api_key", default=None, metavar="KEY",
              help=f"Save this API key (from {KEYS_URL}) after checking it works — no browser, no prompt.")
@click.option("--no-browser", is_flag=True, default=False,
              help="Print auth URL instead of opening browser (step 1 of headless auth).")
@click.option("--session", default=None,
              help="Verify auth session and save API key (step 2 of headless auth).")
@click.option("--json", "json_output", is_flag=True,
              help="Print the result as machine-readable JSON (with --api-key or LIUM_API_KEY; the browser flows print for a person).")
@handle_errors
def init_command(api_key: str | None, no_browser: bool, session: str | None, json_output: bool):
    """Initialize Lium CLI configuration.

    Sets up API key and SSH key configuration. With --api-key (or LIUM_API_KEY
    already exported) nothing opens a browser and nothing waits for a person.

    \b
    Examples:
      lium init --api-key sk_...      # headless: check the key, save it, set up the SSH key
      lium init                       # opens browser for auth
      lium init --no-browser          # prints auth URL + session ID
      lium init --session <ID>        # verifies session and saves API key
    \b
    Without a terminal on stdin (or with LIUM_NONINTERACTIVE=1) `lium init`
    behaves like `--no-browser`. Scripts can skip init entirely by setting
    LIUM_API_KEY, or run `lium init --api-key <key>` once to save a key
    without a browser.
    """
    if api_key is not None and (session or no_browser):
        raise CliFailure(
            "invalid_arguments",
            "--api-key already provides the key; drop --no-browser / --session.",
            EXIT_CONFIGURATION_ERROR,
        )
    if json_output and api_key is None and not _env_key_name():
        # the browser flows talk to a person (URLs, "waiting…"); a machine caller has a key
        raise CliFailure(
            "invalid_arguments",
            f"--json needs --api-key or an exported {API_KEY_ENV_VAR} (or {API_KEY_SECTION_ENV_VAR}); "
            "the browser flows print for a person.",
            EXIT_CONFIGURATION_ERROR,
        )
    # With a workspace named for this command (`-w` / LIUM_WORKSPACE, lium#183) every command runs
    # with the key saved for THAT workspace and nothing else; `init` writes the account key
    # (`[api] api_key`), so an init that exited 0 here would be followed by `No API key is saved
    # for workspace …` on the very next command.
    requested_workspace = os.environ.get("LIUM_WORKSPACE")
    if requested_workspace:
        raise CliFailure(
            "invalid_arguments",
            f"LIUM_WORKSPACE={requested_workspace} is set: commands run with the key saved for that workspace, "
            "which `lium init` does not write. Run `lium keys create <name> --workspace "
            f"{requested_workspace} --save`, or unset LIUM_WORKSPACE to set up the account key.",
            EXIT_CONFIGURATION_ERROR,
        )

    # Headless: the caller has a key — check it, save it, no browser. `is not None`: an empty
    # value (an unset $KEY) is an error to report, not a reason to start the browser flow.
    if api_key is not None:
        save_result = SaveApiKeyAction(api_key=api_key).execute({})
        if not save_result.ok:
            code = save_result.data.get("code", "invalid_api_key")
            exit_code = EXIT_CONFIGURATION_ERROR if code == "empty_api_key" else EXIT_API_ERROR
            # the generic hints point at the saved key ('lium config get api.api_key'); the key
            # checked here came from the flag and nothing was saved
            raise CliFailure(code, save_result.error, exit_code, hint=_FLAG_KEY_HINTS[code])
        ssh_path = _setup_ssh()
        _report("flag", ssh_path, json_output)
        return

    # A key is exported (LIUM_API_API_KEY or LIUM_API_KEY — the SDK reads both, in that order,
    # since lium#136): every command already uses it (the environment wins over the config
    # file), so there is nothing to authenticate — with --session or --no-browser included,
    # where the existing actions would silently do nothing. Say so instead of doing only the
    # SSH half in silence.
    if _env_key_name():
        ssh_path = _setup_ssh()
        _report("env", ssh_path, json_output)
        return

    # Step 2: verify a pending session
    if session:
        verify_action = VerifySessionAction(session_id=session)
        verify_result = verify_action.execute({})
        if not verify_result.ok:
            raise CliFailure("auth_failed", verify_result.error, EXIT_GENERAL_ERROR)
        ssh_path = _setup_ssh()
        _report("config" if verify_result.data.get("already_configured") else "session", ssh_path, json_output)
        return

    # Step 1 (headless): just print URL and exit. A browser nobody can see is
    # no use to a piped caller, so that case takes the headless path too.
    if no_browser or not is_interactive():
        url_action = RequestAuthUrlAction()
        url_result = url_action.execute({})
        if url_result.data.get("already_configured"):
            # a piped `lium init` next to a saved key: say where the key is instead of silence
            ssh_path = _setup_ssh()
            _report("config", ssh_path, json_output)
        return

    # Default: browser flow
    api_action = SetupApiKeyAction()
    api_result = api_action.execute({})

    if not api_result.ok:
        raise CliFailure("auth_failed", f"{api_result.error}. {HEADLESS_HINT}", EXIT_GENERAL_ERROR)

    ssh_path = _setup_ssh()
    _report("config" if api_result.data.get("already_configured") else "browser", ssh_path, json_output)


def _env_key_name() -> str | None:
    """The exported key variable the next command will read, in the SDK's order (``LIUM_API_API_KEY``,
    then ``LIUM_API_KEY`` — ``Config.load``, lium#136); None when neither is set."""
    for name in (API_KEY_SECTION_ENV_VAR, API_KEY_ENV_VAR):
        if os.environ.get(name):
            return name
    return None


def _resolved_source() -> tuple[str, str | None]:
    """Where the next command reads its key — the SDK's own resolution (``Config.load``), which is what
    ``lium whoami --json`` prints — and the workspace whose saved key wins over ``[api] api_key`` when
    ``lium workspaces use`` selected one (``[workspaces] active``, lium#183); None otherwise."""
    try:
        resolved = Config.load()
    except ValueError:
        return config.get_source("api.api_key"), None
    active = resolved.workspace if "[workspace." in resolved.api_key_source else None
    return resolved.api_key_source, active


def _setup_ssh() -> str:
    """Setup SSH key (shared by every flow); returns the configured key path."""
    ssh_action = SetupSshKeyAction()
    ssh_result = ssh_action.execute({})
    if not ssh_result.ok:
        raise CliFailure("ssh_key_setup_failed", ssh_result.error, EXIT_GENERAL_ERROR)
    return config.get("ssh.key_path") or ""


def _report(saved_from: str, ssh_key_path: str, json_output: bool) -> None:
    """One line per fact the caller needs next: where the key came from, where it lives, which SSH key.

    ``saved_from`` is init's own word for how this run got the key (flag/env/config/session/browser);
    ``api_key_source`` in the JSON is the same value ``lium whoami --json`` and ``lium balance --json``
    print — where the next command will read the key from (``env:LIUM_API_KEY`` or ``config:<path> …``).
    """
    config_path = str(config.get_config_path())
    env_name = _env_key_name()
    source, active_workspace = _resolved_source()
    if json_output:
        click.echo(json.dumps({
            "ok": True,
            "api_key_source": source,
            "saved_from": saved_from,
            "env_key": env_name,          # set ⇒ this variable wins over the saved key while exported
            "active_workspace": active_workspace,   # set ⇒ that workspace's saved key wins over [api] api_key
            "config_path": config_path,
            "ssh_key_path": ssh_key_path,
        }, sort_keys=True))
        return
    if saved_from == "flag":
        ui.success(f"API key checked and saved to {escape(config_path)}")
        if env_name:
            ui.warning(f"{escape(env_name)} is set and wins over the saved key while it is exported")
    elif saved_from == "env":
        ui.info(f"Using the API key from {escape(env_name)}; the key is not written to {escape(config_path)}")
    elif saved_from == "config":
        ui.info(f"API key already saved in {escape(config_path)}")
    if active_workspace and saved_from in ("flag", "session", "browser"):
        # the browser and --session flows save an [api] key too: the same warning where the active
        # workspace's saved key, not the one just saved, is what the next command reads
        ui.warning(
            f"`lium workspaces use {escape(active_workspace)}` is in effect: commands run with the key saved for "
            f"'{escape(active_workspace)}', not the key saved now (`lium config unset workspaces.active` to undo)"
        )
    if ssh_key_path:
        ui.info(f"SSH key: {escape(ssh_key_path)}")
