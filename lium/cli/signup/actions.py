"""Signup action: create an account and keep the API key it mints."""

import os
import secrets
import string
from datetime import datetime, timezone

import requests

from lium.cli.actions import ActionResult
from lium.cli.settings import config
from lium.sdk.exceptions import LiumError
from lium.sdk.utils import request_same_origin

PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_"
PASSWORD_LENGTH = 20
REQUEST_TIMEOUT = 30
MINTED_KEY_NAME = "Default"
DEFAULT_BASE_URL = "https://lium.io/api"


def generate_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def base_url() -> str:
    # read at call time so LIUM_BASE_URL can point signup at staging, same as the SDK
    return os.getenv("LIUM_BASE_URL", DEFAULT_BASE_URL)


def _send(method: str, url: str, **kwargs) -> requests.Response:
    # `requests.post` / `requests.get` by name so the module-level functions stay the seam tests replace
    return getattr(requests, method.lower())(url, **kwargs)


def _json_object(response: requests.Response) -> dict:
    # a proxy between us and the backend can answer with valid JSON that is not an object
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _is_expired(expires_at) -> bool:
    # the backend stores naive UTC timestamps, so an offset-aware value is normalised before comparing
    if not isinstance(expires_at, str):
        return False
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    return expiry <= datetime.now(timezone.utc).replace(tzinfo=None)


def _select_minted_key(api_keys: list) -> str | None:
    # GET /keys lists dead keys too — a restored account can carry several rows named "Default"
    usable = [
        k for k in api_keys
        if isinstance(k, dict)
        and k.get("key")
        and k.get("is_active", True)
        and not _is_expired(k.get("expires_at"))
    ]
    if not usable:
        return None

    newest = max(usable, key=lambda k: (k.get("name") == MINTED_KEY_NAME, str(k.get("created_at") or "")))
    return newest.get("key")


class SignupAction:
    """Register an account and return the API key minted for it.

    The account is created by ``POST /users``; the API key that call mints is
    read back with ``POST /users/login`` + ``GET /keys``. Newer backends return
    the key in the signup response itself — when they do, the two extra calls
    are skipped.
    """

    def __init__(self, email: str, password: str, display_name: str):
        self.email = email
        self.password = password
        self.display_name = display_name

    def execute(self, ctx: dict) -> ActionResult:
        # a second account would be unreachable — nothing here can switch between keys
        if config.get("api.api_key"):
            if os.environ.get("LIUM_API_KEY"):
                return ActionResult(
                    ok=False,
                    data={},
                    error="An API key is already configured through the LIUM_API_KEY environment "
                          "variable. Run 'unset LIUM_API_KEY' first, or use 'lium init' to "
                          "re-authenticate.",
                )
            return ActionResult(
                ok=False,
                data={},
                error="An API key is already configured. Run 'lium config unset api.api_key' first, "
                      "or use 'lium init' to re-authenticate.",
            )

        creation_result = self._create_account()
        if not creation_result.ok:
            return creation_result

        api_key = creation_result.data.get("api_key") or self._read_minted_key()
        if not api_key:
            return ActionResult(
                ok=False,
                data={"account_may_exist": True},
                error="Account created, but the API key could not be read back.",
            )

        config.set("api.api_key", api_key)
        return ActionResult(ok=True, data={
            "api_key": api_key,
            "signup_credit_granted": creation_result.data.get("signup_credit_granted"),
        })

    def _create_account(self) -> ActionResult:
        try:
            # same-origin redirects only (DAH-3543): a 307 elsewhere would carry the e-mail and password there
            response = request_same_origin(
                _send,
                "POST",
                f"{base_url()}/users",
                json={"name": self.display_name, "email": self.email, "password": self.password},
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.RequestException, LiumError) as e:
            # the account is created and its mail sent inside the call, so any transport failure
            # after the request left can still leave an account behind
            return ActionResult(
                ok=False,
                data={"account_may_exist": True},
                error=f"Signup request failed: {e}. The account may have been created.",
            )

        if response.status_code == 429:
            return ActionResult(
                ok=False,
                data={},
                error="Too many signups from this network. Wait and retry, or sign up at https://lium.io.",
            )

        if response.status_code >= 400:
            return ActionResult(ok=False, data={}, error=self._describe_failure(response))

        body = _json_object(response)
        return ActionResult(ok=True, data={
            "api_key": body.get("api_key"),
            "signup_credit_granted": body.get("signup_credit_granted"),
        })

    def _read_minted_key(self) -> str | None:
        try:
            login_response = request_same_origin(
                _send,
                "POST",
                f"{base_url()}/users/login",
                json={"email": self.email, "password": self.password},
                timeout=REQUEST_TIMEOUT,
            )
            login_response.raise_for_status()
            token = login_response.json().get("token")
            if not token:
                return None

            keys_response = request_same_origin(
                _send,
                "GET",
                f"{base_url()}/keys",
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT,
            )
            keys_response.raise_for_status()
            api_keys = keys_response.json()
        except (requests.RequestException, LiumError, ValueError):
            return None

        if not isinstance(api_keys, list):
            return None

        return _select_minted_key(api_keys)

    @staticmethod
    def _describe_failure(response: requests.Response) -> str:
        detail = _json_object(response).get("detail")

        if isinstance(detail, dict):
            detail = "; ".join(f"{k}: {v}" for k, v in detail.items())
        return str(detail) if detail else f"Signup failed with HTTP {response.status_code}."
