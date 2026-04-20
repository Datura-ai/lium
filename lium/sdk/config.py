"""Configuration loading for the Lium SDK.

Supports multiple environments (prod, staging, custom) stored in
``~/.lium/config.ini``. The active environment is chosen via, in order:

1. ``LIUM_ENV`` env var
2. ``[general] env = ...`` in config.ini
3. Default: ``prod``

Per-env sections look like::

    [env.prod]
    api_key = sk_...
    base_url = https://lium.io/api
    base_pay_url = https://pay-api.lium.io

    [env.staging]
    api_key = sk_...
    base_url = https://staging.lium.io/api
    base_pay_url = https://pay-api-staging.lium.io

Legacy ``[api] api_key`` (without an env section) is treated as the key
for the ``prod`` environment, so existing configs keep working.

Env vars ``LIUM_API_KEY`` / ``LIUM_BASE_URL`` / ``LIUM_PAY_URL`` always
win over file values for the active environment.
"""

import os
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


# Built-in presets. Users can override any field per-env via `lium env set`.
ENV_PRESETS: Dict[str, Dict[str, str]] = {
    "prod": {
        "base_url": "https://lium.io/api",
        "base_pay_url": "https://pay-api.lium.io",
    },
    "staging": {
        "base_url": "https://staging.lium.io/api",
        "base_pay_url": "https://pay-api-staging.lium.io",
    },
}

DEFAULT_ENV = "prod"


def _config_path() -> Path:
    return Path.home() / ".lium" / "config.ini"


def _read_config() -> ConfigParser:
    cfg = ConfigParser()
    path = _config_path()
    if path.exists():
        cfg.read(path)
    return cfg


def _env_section(env: str) -> str:
    return f"env.{env}"


def resolve_env_name(cfg: Optional[ConfigParser] = None) -> str:
    """Determine the active environment name."""
    env = os.getenv("LIUM_ENV")
    if env:
        return env.strip()
    cfg = cfg or _read_config()
    if cfg.has_section("general") and cfg.has_option("general", "env"):
        return cfg.get("general", "env").strip() or DEFAULT_ENV
    return DEFAULT_ENV


def list_envs(cfg: Optional[ConfigParser] = None) -> List[str]:
    """Return all known env names (presets + user-defined)."""
    cfg = cfg or _read_config()
    names = set(ENV_PRESETS.keys())
    for section in cfg.sections():
        if section.startswith("env."):
            names.add(section[len("env.") :])
    return sorted(names)


@dataclass
class Config:
    api_key: str
    base_url: str = "https://lium.io/api"
    base_pay_url: str = "https://pay-api.lium.io"
    ssh_key_path: Optional[Path] = None
    env: str = DEFAULT_ENV

    @classmethod
    def load(cls) -> "Config":
        """Load config for the active environment."""
        cfg = _read_config()
        env = resolve_env_name(cfg)

        # Start from preset defaults, then overlay file, then env vars.
        preset = ENV_PRESETS.get(env, {})
        base_url = preset.get("base_url", "https://lium.io/api")
        base_pay_url = preset.get("base_pay_url", "https://pay-api.lium.io")
        api_key: Optional[str] = None

        section = _env_section(env)
        if cfg.has_section(section):
            api_key = cfg.get(section, "api_key", fallback=None) or api_key
            base_url = cfg.get(section, "base_url", fallback=base_url)
            base_pay_url = cfg.get(section, "base_pay_url", fallback=base_pay_url)

        # Legacy fallback: [api] api_key — treated as the prod key when the
        # active env is prod and no [env.prod] section exists with a key.
        if not api_key and env == DEFAULT_ENV and cfg.has_section("api"):
            api_key = cfg.get("api", "api_key", fallback=None)

        # Env vars override everything for the active environment.
        api_key = os.getenv("LIUM_API_KEY", api_key)
        base_url = os.getenv("LIUM_BASE_URL", base_url)
        base_pay_url = os.getenv("LIUM_PAY_URL", base_pay_url)

        if not api_key:
            raise ValueError(
                f"No API key found for environment '{env}'. "
                f"Set it with: lium env set {env} --api-key <KEY> "
                f"(or export LIUM_API_KEY / use ~/.lium/config.ini)."
            )

        # Find SSH key with fallback.
        ssh_key: Optional[Path] = None
        for key_name in ["id_ed25519", "id_rsa", "id_ecdsa"]:
            key_path = Path.home() / ".ssh" / key_name
            if key_path.exists():
                ssh_key = key_path
                break

        return cls(
            api_key=api_key,
            base_url=base_url,
            base_pay_url=base_pay_url,
            ssh_key_path=ssh_key,
            env=env,
        )

    @property
    def ssh_public_keys(self) -> List[str]:
        """Get SSH public keys."""
        if not self.ssh_key_path:
            return []
        pub_path = self.ssh_key_path.with_suffix(".pub")
        if pub_path.exists():
            with open(pub_path) as f:
                return [
                    line.strip()
                    for line in f
                    if line.strip().startswith(("ssh-", "ecdsa-"))
                ]
        return []


__all__ = ["Config", "ENV_PRESETS", "DEFAULT_ENV", "resolve_env_name", "list_envs"]
