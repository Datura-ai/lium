import os, sys, time, requests, webbrowser
from typing import Optional

from lium.cli import ui

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

def init_auth() -> tuple[str, str]:
    """Request a new auth session. Returns (browser_url, session_id)."""
    url = "https://lium.io/api/cli-auth/init"
    resp = requests.post(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data["browser_url"], data["session_id"]

def poll_auth(session_id: str, max_attempts: int = 6, interval: int = 5) -> Optional[str]:
    """Poll for auth approval. Returns API key or None on timeout."""
    url = f"https://lium.io/api/cli-auth/poll/{session_id}"
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
    """Open browser for authentication and poll for approval."""
    try:
        browser_url, session_id = init_auth()
    except Exception as e:
        ui.error(f"Failed to request auth URL: {e}")
        return None

    ui.info("Browser opened, waiting for authentication...")

    with quiet_fds():
        result = webbrowser.open(browser_url)

    if not result:
        ui.info(f"Opening browser failed. Please, open the page for authentication: {browser_url}")

    return poll_auth(session_id)
