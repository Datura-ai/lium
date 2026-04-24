"""Configuration loading for the Lium SDK."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Config:
    api_key: str
    base_url: str = "https://lium.io/api"
    base_pay_url: str = "https://pay-api.lium.io"
    ssh_key_path: Optional[Path] = None

    @classmethod
    def load(cls) -> "Config":
        """Load config from env/file with smart defaults."""
        api_key = os.getenv("LIUM_API_KEY")
        if not api_key:
            from configparser import ConfigParser
            config_file = Path.home() / ".lium" / "config.ini"
            if config_file.exists():
                config = ConfigParser()
                config.read(config_file)
                api_key = config.get("api", "api_key", fallback=None)

        if not api_key:
            raise ValueError("No API key found. Set LIUM_API_KEY or ~/.lium/config.ini")

        # Find SSH key with fallback
        ssh_key = None
        for key_name in ["id_ed25519", "id_rsa", "id_ecdsa"]:
            key_path = Path.home() / ".ssh" / key_name
            if key_path.exists():
                ssh_key = key_path
                break

        return cls(
            api_key=api_key,
            base_url=os.getenv("LIUM_BASE_URL", "https://lium.io/api"),
            base_pay_url=os.getenv("LIUM_PAY_URL", "https://pay-api.lium.io"),
            ssh_key_path=ssh_key
        )

    @property
    def ssh_public_keys(self) -> List[str]:
        """Get SSH public keys."""
        if not self.ssh_key_path:
            return []
        pub_path = self.ssh_key_path.with_suffix('.pub')
        if pub_path.exists():
            with open(pub_path) as f:
                return [line.strip() for line in f if line.strip().startswith(('ssh-', 'ecdsa-'))]
        return []


__all__ = ["Config"]
