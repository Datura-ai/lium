"""Configuration loading for the Lium SDK."""

import os
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

API_KEY_ENV_VAR = "LIUM_API_KEY"
# The CLI's generic ``LIUM_<SECTION>_<OPTION>`` spelling of ``[api] api_key``;
# ``ConfigManager.get`` checks it before ``LIUM_API_KEY``, so the SDK does too.
API_KEY_SECTION_ENV_VAR = "LIUM_API_API_KEY"
API_KEY_SOURCE_EXPLICIT = "explicit"
SSH_KEY_ENV_VAR = "LIUM_SSH_KEY_PATH"
DEFAULT_SSH_KEY_NAMES = ("id_ed25519", "id_rsa", "id_ecdsa")


def config_file_path() -> Path:
    return Path.home() / ".lium" / "config.ini"


def _config_option(section: str, option: str) -> Optional[str]:
    """``[section] option`` from the CLI's config file, or None."""
    config_file = config_file_path()
    if not config_file.exists():
        return None
    from configparser import ConfigParser

    config = ConfigParser()
    config.read(config_file)
    return config.get(section, option, fallback=None) or None


def resolve_api_key() -> Tuple[Optional[str], Optional[str]]:
    """The API key the SDK will use and where it came from.

    Returns ``(api_key, source)``; both are ``None`` when no key is configured.
    The order — ``LIUM_API_API_KEY``, ``LIUM_API_KEY``, then ``[api] api_key``
    in the config file — is the CLI's (``ConfigManager.get``), so ``lium
    config get api.api_key`` and the key the SDK sends are the same key.
    ``source`` is ``env:<VAR>`` or ``config:<path> [api] api_key``; it is
    recorded because two commands run in different shells can pick up
    different keys, and an auth error that does not say which key it used
    sends the caller to the wrong place.
    """
    for env_var in (API_KEY_SECTION_ENV_VAR, API_KEY_ENV_VAR):
        api_key = os.getenv(env_var)
        if api_key:
            return api_key, f"env:{env_var}"

    api_key = _config_option("api", "api_key")
    if api_key:
        return api_key, f"config:{config_file_path()} [api] api_key"

    return None, None


def resolve_ssh_key_path() -> Tuple[Optional[Path], Optional[str]]:
    """The private SSH key the SDK will use and where it came from.

    Order: ``LIUM_SSH_KEY_PATH``, then ``[ssh] key_path`` in the config file
    (what ``lium init`` writes), then the first of ``~/.ssh/id_ed25519``,
    ``id_rsa``, ``id_ecdsa`` that exists. A configured path is returned even
    when the file is missing, so the failure names the key the user chose
    instead of silently using another one. ``source`` is ``env:<VAR>``,
    ``config:<path> [ssh] key_path`` or ``default:<path>``.
    """
    configured = os.getenv(SSH_KEY_ENV_VAR)
    if configured:
        return Path(configured).expanduser(), f"env:{SSH_KEY_ENV_VAR}"

    configured = _config_option("ssh", "key_path")
    if configured:
        return Path(configured).expanduser(), f"config:{config_file_path()} [ssh] key_path"

    for key_name in DEFAULT_SSH_KEY_NAMES:
        key_path = Path.home() / ".ssh" / key_name
        if key_path.exists():
            return key_path, f"default:{key_path}"

    return None, None


def api_key_fingerprint(api_key: Optional[str]) -> str:
    """A short, non-secret handle for a key: first six and last four characters."""
    if not api_key:
        return "none"
    if len(api_key) <= 12:
        return "***"
    return f"{api_key[:6]}…{api_key[-4:]}"


def _read_config_file() -> ConfigParser:
    # no interpolation: a workspace name such as "100% GPU" is an opaque string, not a %-template
    parser = ConfigParser(interpolation=None)
    path = config_file_path()
    if path.exists():
        parser.read(path)
    return parser


def workspace_section(name: str) -> str:
    """The config.ini section holding one workspace's id and API key: ``[workspace.<name>]`` (lower-cased).

    A section header is one line: a name with a line break or another control character cannot be
    saved and is refused (``ValueError``) before anything is written.
    """
    if any(ch < " " or ch == "\x7f" for ch in name):
        raise ValueError(f"Workspace name {name!r} has control characters and cannot be saved in config.ini")
    return f"workspace.{name.lower()}"


@dataclass
class Config:
    api_key: str
    base_url: str = "https://lium.io/api"
    base_pay_url: str = "https://pay-api.lium.io"
    ssh_key_path: Optional[Path] = None
    # The workspace this client was asked to act in (``--workspace`` / LIUM_WORKSPACE /
    # ``lium workspaces use``), by the name it was saved under; None when nothing was asked.
    workspace: Optional[str] = None
    # Its id from ``[workspace.<name>] id`` — set only when that section's saved key is the key in use,
    # and then what the key is compared against, so a rename on the server does not turn it into
    # "another workspace's key".
    workspace_id: Optional[str] = None
    # True when ``workspace`` came from ``--workspace`` / LIUM_WORKSPACE for this command, False when it
    # is the stored default (``lium workspaces use``) or nothing was asked.
    workspace_explicit: bool = False
    # A browser-session token for the few routes that refuse API keys (``/workspaces`` writes,
    # ``/keys``): LIUM_SESSION_TOKEN or ``[session] token`` written by ``lium workspaces login``.
    session_token: Optional[str] = None
    # Where ``api_key`` came from (``env:LIUM_API_KEY``, ``config:<path> [api] api_key``,
    # ``config:<path> [workspace.<name>] api_key``): what an auth error names (DAH-2872).
    api_key_source: str = API_KEY_SOURCE_EXPLICIT
    ssh_key_source: Optional[str] = None

    @classmethod
    def load(cls, workspace: Optional[str] = None, *, key_for_workspace: bool = True) -> "Config":
        """Load config from env/file with smart defaults.

        An explicitly requested workspace (``workspace=`` or LIUM_WORKSPACE) means the key saved for
        it (``[workspace.<name>] api_key``) and nothing else: a key acts in exactly one workspace, so
        falling back to another key would run the command in another team on another balance;
        ``ValueError`` when none is saved. Otherwise, first match wins: LIUM_API_API_KEY / LIUM_API_KEY, the key saved for
        the workspace selected with ``lium workspaces use`` (``[workspaces] active``), ``[api] api_key``.
        A config without workspace sections resolves as it always has.

        ``key_for_workspace=False`` is for the commands that manage teams (``lium workspaces``,
        ``lium keys``): a missing key for the requested workspace is not an error there — the key is
        LIUM_API_API_KEY / LIUM_API_KEY, else the one saved for the requested workspace, else the one saved for the stored
        default, else ``[api] api_key`` — so ``lium keys create … --save`` and
        ``LIUM_API_KEY=… lium workspaces use …``, the remedies for a missing key, work with LIUM_WORKSPACE
        exported, and ``lium -w W workspaces members`` reads with W's key when one is saved.
        """
        file_config = _read_config_file()
        requested = workspace or os.getenv("LIUM_WORKSPACE") or None
        active = file_config.get("workspaces", "active", fallback=None)

        def saved(name: str) -> Tuple[Optional[str], str]:
            section = workspace_section(name)
            return file_config.get(section, "api_key", fallback=None), f"config:{config_file_path()} [{section}] api_key"

        saved_for_requested, requested_source = saved(requested) if requested else (None, "")
        api_key, source = None, None
        if requested and key_for_workspace:
            api_key, source = saved_for_requested, requested_source
            if not api_key:
                raise ValueError(
                    f"No API key is saved for workspace '{requested}'; run "
                    f"`lium keys create <name> --workspace {requested} --save` "
                    f"(or `LIUM_API_KEY=<a key bound to it> lium workspaces use {requested}`)"
                )
        else:
            for env_var in (API_KEY_SECTION_ENV_VAR, API_KEY_ENV_VAR):   # the CLI's order (DAH-2896)
                api_key = os.getenv(env_var)
                if api_key:
                    source = f"env:{env_var}"
                    break
            if not api_key and saved_for_requested:
                api_key, source = saved_for_requested, requested_source
            if not api_key and active:
                api_key, source = saved(active)
            if not api_key:
                api_key = file_config.get("api", "api_key", fallback=None)
                source = f"config:{config_file_path()} [api] api_key"

        if not api_key:
            raise ValueError(f"No API key found. Set {API_KEY_ENV_VAR} or {config_file_path()}")

        ssh_key, ssh_source = resolve_ssh_key_path()

        named = requested or active
        section = workspace_section(named) if named else None
        runs_with_saved_key = section is not None and api_key == file_config.get(section, "api_key", fallback=None)

        return cls(
            api_key=api_key,
            base_url=os.getenv("LIUM_BASE_URL", "https://lium.io/api"),
            base_pay_url=os.getenv("LIUM_PAY_URL", "https://pay-api.lium.io"),
            ssh_key_path=ssh_key,
            workspace=named,
            workspace_id=file_config.get(section, "id", fallback=None) if runs_with_saved_key else None,
            workspace_explicit=bool(requested),
            session_token=os.getenv("LIUM_SESSION_TOKEN") or file_config.get("session", "token", fallback=None),
            api_key_source=source or API_KEY_SOURCE_EXPLICIT,
            ssh_key_source=ssh_source,
        )

    @property
    def api_key_fingerprint(self) -> str:
        return api_key_fingerprint(self.api_key)

    @property
    def api_key_description(self) -> str:
        """``key <fingerprint> from <source>`` — what an auth error should name."""
        return f"key {self.api_key_fingerprint} from {self.api_key_source}"

    @property
    def ssh_public_keys(self) -> List[str]:
        """Get SSH public keys."""
        if not self.ssh_key_path:
            return []
        # `.pub` is appended, not swapped in: a configured `lium.ed25519` must read `lium.ed25519.pub`,
        # not `lium.pub` (with_suffix would replace the dotted part of the name)
        pub_path = self.ssh_key_path.with_name(self.ssh_key_path.name + '.pub')
        if pub_path.exists():
            with open(pub_path) as f:
                return [line.strip() for line in f if line.strip().startswith(('ssh-', 'ecdsa-'))]
        return []


__all__ = [
    "Config",
    "API_KEY_ENV_VAR",
    "API_KEY_SECTION_ENV_VAR",
    "SSH_KEY_ENV_VAR",
    "api_key_fingerprint",
    "config_file_path",
    "resolve_api_key",
    "resolve_ssh_key_path",
    "workspace_section",
]
