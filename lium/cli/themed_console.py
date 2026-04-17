"""Themed console with automatic dark/light detection."""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any
from rich.console import Console as RichConsole

from lium.cli.settings import config


class ThemedConsole(RichConsole):
    """Console with theme support and semantic color methods."""
    
    def __init__(self):
        super().__init__()
        self.themes = self._load_themes()
        self.current_theme_name = config.get('ui.theme', 'auto')
        self.theme = self._resolve_theme()
    
    def _load_themes(self) -> Dict[str, Dict[str, str]]:
        """Load themes from themes.json."""
        candidates = [Path(__file__).parent / "themes.json"]
        meipass_root = getattr(sys, "_MEIPASS", None)
        if meipass_root:
            candidates.append(Path(meipass_root) / "lium" / "cli" / "themes.json")

        for themes_file in candidates:
            try:
                with open(themes_file, encoding="utf-8") as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                continue

        # Fallback to basic themes
        return {
            "dark": {
                "success": "green",
                "error": "red",
                "warning": "yellow",
                "info": "cyan",
                "pending": "cyan",
                "id": "dim",
                "dim": "dim",
            },
            "light": {
                "success": "green",
                "error": "red",
                "warning": "yellow",
                "info": "blue",
                "pending": "blue",
                "id": "dim",
                "dim": "dim",
            },
        }
    
    
    def _resolve_theme(self) -> Dict[str, str]:
        """Resolve theme name to actual theme dict."""
        if self.current_theme_name == "auto":
            detected = "dark" if self._is_dark_terminal() else "light"
            return self.themes.get(detected, self.themes["dark"])
        return self.themes.get(self.current_theme_name, self.themes["dark"])
    
    def _is_dark_terminal(self) -> bool:
        """Detect if terminal has dark background."""
        # Try macOS system theme detection first
        if platform.system() == 'Darwin':
            try:
                result = subprocess.run(
                    ['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                # If AppleInterfaceStyle exists and equals "Dark", it's dark mode
                if result.returncode == 0 and result.stdout.strip() == "Dark":
                    return True
                elif result.returncode != 0:
                    # Key doesn't exist = light mode
                    return False
            except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                # Fall through to other detection methods
                pass
        
        # Check COLORFGBG environment variable (works in some terminals)
        colorfgbg = os.environ.get('COLORFGBG', '')
        if colorfgbg and ';' in colorfgbg:
            try:
                bg = colorfgbg.split(';')[-1]
                if bg.isdigit():
                    # Background colors 0-7 are typically dark
                    return int(bg) <= 7
            except (ValueError, IndexError):
                pass
        
        # Check for dark mode hints in environment
        if os.environ.get('THEME', '').lower() in ['dark', 'dracula', 'monokai', 'nord']:
            return True
        
        # Default to light for better safety (less eye strain if wrong)
        return False
    
    def _colorized_print(self, text, style_key: str, **kwargs) -> None:
        """Print text with color from current theme."""
        if isinstance(text, str):
            styled_text = self.get_styled(text, style_key)
            self.print(styled_text, **kwargs)
        else:
            # For Rich objects (Table, Text, etc), print directly
            self.print(text, **kwargs)
    
    
    # Semantic color methods
    def success(self, text, **kwargs) -> None:
        """Print success message."""
        self._colorized_print(text, 'success', **kwargs)
    
    def error(self, text, **kwargs) -> None:
        """Print error message."""
        self._colorized_print(text, 'error', **kwargs)
    
    def warning(self, text, **kwargs) -> None:
        """Print warning message."""
        self._colorized_print(text, 'warning', **kwargs)
    
    def info(self, text, **kwargs) -> None:
        """Print info message."""
        self._colorized_print(text, 'info', **kwargs)
    
    def pending(self, text, **kwargs) -> None:
        """Print pending status message."""
        self._colorized_print(text, 'pending', **kwargs)
    
    def dim(self, text, **kwargs) -> None:
        """Print dimmed text."""
        self._colorized_print(text, 'dim', **kwargs)

    def id(self, text: str, **kwargs) -> None:
        """Print ID text (dimmed by default)."""
        self._colorized_print(text, 'id', **kwargs)
    
    def pod_status_color(self, status: str) -> str:
        """Get color for pod status."""
        status_key = status.lower()
        return self.theme.get(status_key, self.theme.get('dim', 'dim'))
    
    def get_styled(self, text: str, style_key: str) -> str:
        """Get styled text without printing."""
        color = self.theme.get(style_key, self.theme.get('dim', 'dim'))
        return f"[{color}]{text}[/{color}]"
    
    def switch_theme(self, theme_name: str) -> None:
        """Switch to a different theme and save to config."""
        if theme_name not in ['auto', 'dark', 'light']:
            raise ValueError(f"Unknown theme: {theme_name}")
        
        self.current_theme_name = theme_name
        self.theme = self._resolve_theme()
        config.set('ui.theme', theme_name)
    
    def get_current_theme_name(self) -> str:
        """Get current theme name."""
        return self.current_theme_name
    
    def get_resolved_theme_name(self) -> str:
        """Get the actual resolved theme name (useful for 'auto' mode)."""
        if self.current_theme_name == "auto":
            return "dark" if self._is_dark_terminal() else "light"
        return self.current_theme_name
