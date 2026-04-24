import subprocess
from pathlib import Path

from lium.cli.actions import ActionResult
from .auth import browser_auth, init_auth, poll_auth
from lium.cli.settings import config
from lium.cli import ui


class SetupApiKeyAction:
    """Setup API key using browser authentication."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute API key setup with browser flow."""
        current_key = config.get('api.api_key')
        if current_key:
            return ActionResult(ok=True, data={"already_configured": True})

        api_key = browser_auth()

        if not api_key:
            return ActionResult(ok=False, data={}, error="Authentication failed")

        config.set('api.api_key', api_key)
        return ActionResult(ok=True, data={"already_configured": False})


class RequestAuthUrlAction:
    """Request auth URL and print it (step 1 of headless auth)."""

    def execute(self, ctx: dict) -> ActionResult:
        current_key = config.get('api.api_key')
        if current_key:
            return ActionResult(ok=True, data={"already_configured": True})

        try:
            browser_url, session_id = init_auth()
        except Exception as e:
            return ActionResult(ok=False, data={}, error=f"Failed to request auth URL: {e}")

        ui.info("Open this URL to authenticate:")
        ui.print(f"\n  {browser_url}\n")
        ui.info(f"Then complete authentication with:")
        ui.print(f"\n  lium init --session {session_id}\n")

        return ActionResult(ok=True, data={"session_id": session_id})


class VerifySessionAction:
    """Verify auth session and save API key (step 2 of headless auth)."""

    def __init__(self, session_id: str):
        self.session_id = session_id

    def execute(self, ctx: dict) -> ActionResult:
        current_key = config.get('api.api_key')
        if current_key:
            return ActionResult(ok=True, data={"already_configured": True})

        ui.dim("Checking authentication status...")
        api_key = poll_auth(self.session_id, max_attempts=12, interval=5)

        if not api_key:
            return ActionResult(ok=False, data={}, error="Authentication not approved yet. Make sure you opened the URL and approved access.")

        config.set('api.api_key', api_key)
        ui.success("API key saved")
        return ActionResult(ok=True, data={"already_configured": False})


class SetupSshKeyAction:
    """Setup SSH key path in config."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute SSH key setup."""
        if config.get('ssh.key_path'):
            return ActionResult(ok=True, data={"already_configured": True})

        ssh_dir = Path.home() / ".ssh"
        available_keys = [
            ssh_dir / key_name
            for key_name in ["id_ed25519", "id_rsa", "id_ecdsa"]
            if (ssh_dir / key_name).exists()
        ]

        if not available_keys:
            key_path = ssh_dir / "id_ed25519"
            try:
                subprocess.run(
                    ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
                    check=True, capture_output=True
                )
                selected_key = key_path
            except Exception as e:
                return ActionResult(ok=False, data={}, error=f"Failed to generate SSH key: {e}")
        else:
            selected_key = available_keys[0]

        config.set('ssh.key_path', str(selected_key))
        return ActionResult(ok=True, data={"already_configured": False})
