"""Configuration management for Lium CLI."""

import os
import json
from configparser import ConfigParser
from pathlib import Path
from typing import Any, Optional, Dict, List
from rich.prompt import Prompt

from lium.cli.interactive import is_interactive, noninteractive_reason

# Delayed import to avoid circular dependency


class ConfigManager:
    """Centralized configuration manager for Lium CLI."""
    
    def __init__(self):
        self.config_dir = self._ensure_config_dir()
        self.config_file = self.config_dir / "config.ini"
        self._config = self._load_config()

    @property
    def default_template_id(self) -> Optional[str]:
        DEFAULT_TEMPLATE_ID = "1948937e-5049-47ad-8e26-bcf1a4549d70"
        return DEFAULT_TEMPLATE_ID

    @property
    def default_backup_path(self) -> str:
        """Default backup path."""
        return "/root"
    
    @property
    def default_backup_frequency(self) -> int:
        """Default backup frequency in hours."""
        return 6
    
    @property
    def default_backup_retention(self) -> int:
        """Default backup retention in days."""
        return 7
    
    def _ensure_config_dir(self) -> Path:
        """Ensure ~/.lium directory exists."""
        config_dir = Path.home() / ".lium"
        # parents=True: in a fresh container HOME may name a directory nothing has created yet,
        # and without it every command died at import with FileNotFoundError
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir
    
    def _load_config(self) -> ConfigParser:
        """Load configuration from file (no %-interpolation: values such as workspace names are opaque)."""
        config = ConfigParser(interpolation=None)
        if self.config_file.exists():
            config.read(self.config_file)
        return config
    
    def _save_config(self) -> None:
        """Save configuration to file."""
        with open(self.config_file, 'w') as f:
            self._config.write(f)
        # the file holds the API key — keep it readable by the owner only
        os.chmod(self.config_file, 0o600)
    
    def _parse_key(self, key: str) -> tuple[str, str]:
        """Parse key into section and option."""
        if '.' in key:
            section, option = key.split('.', 1)
        else:
            section = 'default'
            option = key
        return section, option
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get configuration value."""
        section, option = self._parse_key(key)
        
        # Check environment variable first (uppercase with LIUM_ prefix)
        env_key = f"LIUM_{key.upper().replace('.', '_')}"
        env_value = os.environ.get(env_key)
        if env_value is not None:
            return env_value
        if key == "api.api_key":
            env_value = os.environ.get("LIUM_API_KEY")
            if env_value is not None:
                return env_value
        
        try:
            return self._config.get(section, option)
        except Exception:  # Catch all configparser exceptions
            return default
    
    def get_source(self, key: str) -> Optional[str]:
        """Where :meth:`get` would read ``key`` from, in the same precedence order.

        ``env:<VAR>`` or ``config:<path> [section] option``; None when unset.
        """
        section, option = self._parse_key(key)

        env_key = f"LIUM_{key.upper().replace('.', '_')}"
        if os.environ.get(env_key) is not None:
            return f"env:{env_key}"
        if key == "api.api_key" and os.environ.get("LIUM_API_KEY") is not None:
            return "env:LIUM_API_KEY"

        if self._config.has_option(section, option):
            return f"config:{self.config_file} [{section}] {option}"
        return None

    def set(self, key: str, value: str) -> None:
        """Set configuration value."""
        section, option = self._parse_key(key)
        
        # Handle special interactive keys
        if key == 'template.default_id' and not value:
            self._require_terminal_for(key)
            value = self._select_template()
            if not value:
                return
        elif key == 'ui.theme' and not value:
            self._require_terminal_for(key)
            value = self._select_theme()
            if not value:
                return
        elif key == 'api.api_key' and not value:
            self._require_terminal_for(key)
            value = self._input_api_key()
            if not value:
                return
        
        if not self._config.has_section(section):
            self._config.add_section(section)
        
        self._config.set(section, option, value)
        self._save_config()
    
    def get_in_section(self, section: str, option: str) -> Optional[str]:
        """Read an option from a section whose name itself contains a dot (``[workspace.<name>]``)."""
        return self._config.get(section, option, fallback=None)

    def set_in_section(self, section: str, option: str, value: str) -> None:
        """Set an option in a section whose name itself contains a dot (``[workspace.<name>]``)."""
        if not self._config.has_section(section):
            self._config.add_section(section)
        self._config.set(section, option, value)
        self._save_config()

    def sections(self, prefix: str) -> list:
        """The section names starting with ``prefix`` (``"workspace."`` for the saved workspaces)."""
        return [name for name in self._config.sections() if name.startswith(prefix)]

    def remove_section(self, section: str) -> bool:
        """Drop a whole section (``[workspace.<name>]`` after `lium workspaces delete`); False when absent."""
        removed = self._config.remove_section(section)
        if removed:
            self._save_config()
        return removed

    def unset(self, key: str) -> bool:
        """Remove configuration value."""
        section, option = self._parse_key(key)
        
        if self._config.has_section(section):
            if self._config.has_option(section, option):
                self._config.remove_option(section, option)
                # Remove section if empty
                if not self._config.options(section):
                    self._config.remove_section(section)
                self._save_config()
                return True
        return False
    
    def get_all(self) -> Dict[str, Dict[str, str]]:
        """Get all configuration values."""
        result = {}
        for section in self._config.sections():
            result[section] = dict(self._config.items(section))
        return result
    
    def get_config_path(self) -> Path:
        """Get path to configuration file."""
        return self.config_file
    
    @staticmethod
    def _require_terminal_for(key: str) -> None:
        """Interactive selection needs a person; without one, name the value to pass."""
        if is_interactive():
            return
        from .utils import CliFailure, EXIT_CONFIGURATION_ERROR  # Local import to avoid circular dependency

        raise CliFailure(
            "input_required",
            f"'{key}' has no value and cannot be asked for because {noninteractive_reason()}. "
            f"Pass it explicitly: lium config set {key} <VALUE>",
            EXIT_CONFIGURATION_ERROR,
        )

    def _select_template(self) -> Optional[str]:
        """Interactive template selection."""
        from .utils import console  # Local import to avoid circular dependency
        
        try:
            from lium.sdk import Lium
            client = Lium()
            templates = client.templates()
            
            if not templates:
                console.warning("No templates available")
                return None
            
            choices = []
            console.info("Available templates:")
            for i, template in enumerate(templates, 1):
                console.info(f"  {i}. {template.id} - {template.name}")
                choices.append(str(i))
            
            choice = Prompt.ask(
                "Select template",
                choices=choices,
                default="1"
            )
            
            selected_template = templates[int(choice) - 1]
            return selected_template.id
            
        except Exception as e:
            console.error(f"Failed to load templates: {e}")
            return None
    
    def _select_theme(self) -> Optional[str]:
        """Interactive theme selection."""
        from .utils import console  # Local import to avoid circular dependency
        
        themes = ["auto", "dark", "light"]
        
        console.info("Available themes:")
        for i, theme in enumerate(themes, 1):
            console.info(f"  {i}. {theme}")
        
        choice = Prompt.ask(
            "Select theme",
            choices=["1", "2", "3"],
            default="1"
        )
        
        return themes[int(choice) - 1]
    
    def _input_api_key(self) -> Optional[str]:
        """Interactive API key input."""
        from .utils import console  # Local import to avoid circular dependency
        
        api_key = Prompt.ask(
            "[cyan]Enter your Lium API key (get from https://lium.io/api-keys)[/cyan]",
            password=True
        )
        
        if not api_key:
            console.error("No API key provided")
            return None
        
        return api_key
    
    def get_or_ask(self, key: str, prompt_text: str, password: bool = False, default: Optional[str] = None) -> str:
        """Get config value or ask user if not set.

        Without a terminal the question is skipped: ``default`` is used when
        given, otherwise the caller must supply the value another way.
        """
        value = self.get(key)
        if not value:
            if not is_interactive():
                if default is None:
                    self._require_terminal_for(key)
                return default
            value = Prompt.ask(prompt_text, password=password, default=default)
            if value:
                self.set(key, value)
        return value


# Global config instance
config = ConfigManager()


def get_config_value(key: str) -> Optional[str]:
    """Get configuration value (backward compatibility)."""
    return config.get(key)


def get_config_path() -> Path:
    """Get configuration file path (backward compatibility)."""
    return config.get_config_path()
