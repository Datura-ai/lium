import os
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


class SaveApiKeyAction:
    """Check a caller-supplied API key against the API, then save it (headless auth).

    ``data["code"]`` names the failure: ``empty_api_key``, ``invalid_api_key`` (the API refused
    the key), ``api_unreachable`` (no answer, or an answer that is not about the key).
    """

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    def execute(self, ctx: dict) -> ActionResult:
        if not self.api_key:
            return ActionResult(ok=False, data={"code": "empty_api_key"}, error="The API key is empty.")
        if any(ch.isspace() or not ch.isprintable() for ch in self.api_key):
            # a pasted key with a stray newline inside would otherwise be echoed back by the HTTP
            # layer's "invalid header value" error, and retried three times first
            return ActionResult(
                ok=False, data={"code": "invalid_api_key"},
                error="The API key contains whitespace or control characters; paste it as one line.",
            )

        # `lium ls` answers any key, so the check is /users/me — the first call that needs the key
        # to be right. Config.load() is not used: it would read the environment or the file, not
        # the key the caller just passed.
        import requests

        from lium.sdk import Lium, LiumAuthError, LiumError
        from lium.sdk.config import Config

        client = Lium(Config(
            api_key=self.api_key,
            api_key_source="--api-key",   # a 401 then reads "key sk_…abcd from --api-key", not "from explicit"
            base_url=os.getenv("LIUM_BASE_URL", Config.base_url),
            base_pay_url=os.getenv("LIUM_PAY_URL", Config.base_pay_url),
        ), source="cli")
        try:
            client.balance()
        except LiumAuthError as e:
            return ActionResult(
                ok=False, data={"code": "invalid_api_key"},
                error=f"The API key was refused by {client.config.base_url}: {e}",
            )
        except (LiumError, requests.RequestException) as e:
            # the SDK maps 4xx/5xx to LiumError subclasses and lets transport errors through raw
            return ActionResult(
                ok=False, data={"code": "api_unreachable"},
                error=f"Could not check the API key against {client.config.base_url}: {e}",
            )

        config.set("api.api_key", self.api_key)
        return ActionResult(ok=True, data={"already_configured": False})


class RequestAuthUrlAction:
    """Request auth URL and print it (step 1 of headless auth)."""

    def execute(self, ctx: dict) -> ActionResult:
        current_key = config.get('api.api_key')
        if current_key:
            return ActionResult(ok=True, data={"already_configured": True})

        browser_url, session_id = init_auth()

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
                # ssh-keygen refuses to create the key when ~/.ssh is missing —
                # the normal state of a fresh machine or container
                ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
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
