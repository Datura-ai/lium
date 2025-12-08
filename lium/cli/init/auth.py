import os, sys, time, requests, webbrowser
from typing import Optional

from lium.cli import ui
from lium.sdk.config import Config

config = Config.load()

class quiet_fds:
    """Redirect stdout/stderr to /dev/null (silences child processes)."""
    def __enter__(self):
        self._null = open(os.devnull, 'w')
        self._stdout, self._stderr = os.dup(1), os.dup(2)
        os.dup2(self._null.fileno(), 1)
        os.dup2(self._null.fileno(), 2)
        return self
    def __exit__(self, *_):
        os.dup2(self._stdout, 1)
        os.dup2(self._stderr, 2)
        os.close(self._stdout); os.close(self._stderr)
        self._null.close()

def init_auth():
    url = f"{config.base_url}/cli-auth/init"
    resp = requests.post(url,
        json={"callback_url": "http://localhost:8080/auth/callback"},
        headers={"Content-Type": "application/json"},
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()["browser_url"], resp.json()["session_id"]

def poll_auth(session_id, max_attempts=6, interval=5) -> Optional[str]:  # 30 seconds timeout (6 * 5)
    url = f"{config.base_url}/cli-auth/poll/{session_id}"
    for _ in range(max_attempts):
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "approved":
                    return data.get("api_key")
        except KeyboardInterrupt:
            return None
        except Exception:
            pass
        time.sleep(interval)
    return None

def browser_auth() -> Optional[str]:
    """Execute browser authentication flow and return API key or None."""
    try:
        browser_url, session_id = init_auth()

        # Clear messaging about what's happening
        ui.info("Browser opened, waiting for authentication...")

        with quiet_fds():
            result = webbrowser.open(browser_url)

        if not result:
            ui.info(f"Opening browser failed. Please, open the page for authentication: {browser_url}")

        return poll_auth(session_id)
    except Exception:
        return None
