"""Shared helpers for `lium env` commands."""

from configparser import ConfigParser
from pathlib import Path
from typing import Dict

from lium.sdk.config import (
    DEFAULT_ENV,
    ENV_PRESETS,
    _config_path,
    _env_section,
    _read_config,
    list_envs,
    resolve_env_name,
)


def config_path() -> Path:
    return _config_path()


def read_config() -> ConfigParser:
    return _read_config()


def save_config(cfg: ConfigParser) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        cfg.write(f)


def env_section(env: str) -> str:
    return _env_section(env)


def active_env() -> str:
    return resolve_env_name()


def known_envs() -> list[str]:
    return list_envs()


def env_values(cfg: ConfigParser, env: str) -> Dict[str, str]:
    """Resolve the effective values for an env: preset overlaid with file.

    Includes the legacy ``[api] api_key`` fallback for the prod env so the
    list/show commands report accurately for existing configs.
    """
    values = dict(ENV_PRESETS.get(env, {}))
    section = _env_section(env)
    if cfg.has_section(section):
        for key, val in cfg.items(section):
            values[key] = val
    # Legacy fallback for prod
    if env == DEFAULT_ENV and "api_key" not in values and cfg.has_section("api"):
        legacy = cfg.get("api", "api_key", fallback=None)
        if legacy:
            values["api_key"] = legacy
    return values


def set_active_env(cfg: ConfigParser, env: str) -> None:
    if not cfg.has_section("general"):
        cfg.add_section("general")
    cfg.set("general", "env", env)


def mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "…"
    return f"{value[:6]}…{value[-4:]}"


__all__ = [
    "DEFAULT_ENV",
    "ENV_PRESETS",
    "active_env",
    "config_path",
    "env_section",
    "env_values",
    "known_envs",
    "mask_key",
    "read_config",
    "save_config",
    "set_active_env",
]
