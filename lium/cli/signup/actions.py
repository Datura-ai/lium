"""Signup action: create an account and keep the API key it mints."""

import hashlib
import os
import secrets
import string
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from lium.cli.actions import ActionResult
from lium.cli.settings import config
from lium.sdk.exceptions import LiumError
from lium.sdk.utils import request_same_origin

PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_"
PASSWORD_LENGTH = 20
REQUEST_TIMEOUT = 30
MINTED_KEY_NAME = "Default"
BILLING_KEY_NAME = "agent-billing"
BILLING_KEY_OPTION = "api.billing_api_key"
# which rent key the saved billing key was minted next to (a hash, never the key): one account
BILLING_KEY_BOUND_OPTION = "api.billing_key_for"
FINGERPRINT_OPTION = "account.fingerprint"
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


def rent_key_tag(rent_key: str | None) -> str | None:
    return hashlib.sha256(rent_key.encode()).hexdigest()[:16] if rent_key else None


def refuse_if_key_configured() -> ActionResult | None:
    # a second account would be unreachable — nothing here can switch between keys
    for option, what in ((FINGERPRINT_OPTION, "an earlier account's fingerprint (its only login)"),
                         (BILLING_KEY_OPTION, "an earlier account's billing key")):
        if config.get(option):
            return ActionResult(
                ok=False,
                data={},
                error=f"~/.lium/config.ini already holds {what} as {option}. Keep a copy "
                      f"('lium config get {option}' shows it masked; the file has it in full), then "
                      f"'lium config unset {option}' and sign up again.",
            )
    if not config.get("api.api_key"):
        return None
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


def session_token(login_path: str, credentials: dict) -> tuple[str | None, str | None]:
    """A session token from `POST <login_path>`: ``(token, None)``, or ``(None, why)``."""
    try:
        response = request_same_origin(
            _send, "POST", f"{base_url()}{login_path}", json=credentials, timeout=REQUEST_TIMEOUT
        )
    except (requests.RequestException, LiumError) as e:
        return None, f"login request failed: {e}"
    if response.status_code >= 400:
        return None, f"login failed with HTTP {response.status_code}"
    token = _json_object(response).get("token")
    return (token, None) if token else (None, "login returned no session token")


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
        refused = refuse_if_key_configured()
        if refused:
            return refused

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


class FingerprintSignupAction:
    """Register an account with no e-mail and no password (``POST /auth/signup``) and keep its API key.

    The server returns a ``fingerprint``, a random 32-character string that is the account's only login
    (dashboard, ``POST /auth/login``) and is shown once; it is kept as ``[account] fingerprint`` in the
    0600 config file. When the server could not mint a key in the same call (best effort there), one is
    read back or minted through a session.
    """

    def execute(self, ctx: dict) -> ActionResult:
        refused = refuse_if_key_configured()
        if refused:
            return refused
        try:
            response = request_same_origin(_send, "POST", f"{base_url()}/auth/signup", json={}, timeout=REQUEST_TIMEOUT)
        except (requests.RequestException, LiumError) as e:
            return ActionResult(ok=False, data={}, error=f"Signup request failed: {e}. No account was confirmed.")
        if response.status_code == 429:
            return ActionResult(
                ok=False, data={},
                error="Too many e-mail-less signups from this network today. Wait, or use --email.",
            )
        if response.status_code >= 400:
            return ActionResult(ok=False, data={}, error=SignupAction._describe_failure(response))

        body = _json_object(response)
        fingerprint = body.get("fingerprint")
        if not fingerprint:
            return ActionResult(ok=False, data={}, error="Signup answered without the account's fingerprint.")
        # the fingerprint first: it is the only way back into the account, whatever happens next
        config.set(FINGERPRINT_OPTION, fingerprint)

        api_key = body.get("api_key") or self._mint_key(fingerprint)
        data = {
            "fingerprint": fingerprint,
            "user_id": body.get("user_id"),
            "username": body.get("username"),
            "signup_credit_granted": body.get("signup_credit_granted"),
        }
        if not api_key:
            return ActionResult(
                ok=False,
                data={**data, "account_may_exist": True},
                error="Account created, but no API key could be minted. Log in at https://lium.io with the "
                      "fingerprint and create one.",
            )
        config.set("api.api_key", api_key)
        return ActionResult(ok=True, data={**data, "api_key": api_key})

    @staticmethod
    def _mint_key(fingerprint: str) -> str | None:
        token, _ = session_token("/auth/login", {"fingerprint": fingerprint})
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        try:
            listed = request_same_origin(_send, "GET", f"{base_url()}/keys", headers=headers, timeout=REQUEST_TIMEOUT)
            existing = listed.json() if listed.status_code < 400 else []
            key = _select_minted_key(existing) if isinstance(existing, list) else None
            if key:
                return key
            minted = request_same_origin(
                _send, "POST", f"{base_url()}/keys", headers=headers, json={"name": MINTED_KEY_NAME},
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.RequestException, LiumError, ValueError):
            return None
        body = _json_object(minted) if minted.status_code < 400 else {}
        return body.get("key") or body.get("api_key")


class MintBillingKeyAction:
    """Mint a key holding only the `billing` scope for a new account and keep it as ``[api] billing_api_key``.

    A key that pays is not a key that rents: the server grants `billing` alone and only to a signed-in
    session (``POST /keys``), so this logs in with the credentials the account was just made with (e-mail
    and password, or the fingerprint). The key can open card payment pages and crypto invoices and read
    the balance; it reaches no pods.
    """

    def __init__(self, email: str | None = None, password: str | None = None, fingerprint: str | None = None):
        if fingerprint:
            self.login = ("/auth/login", {"fingerprint": fingerprint})
        else:
            self.login = ("/users/login", {"email": email, "password": password})

    def execute(self, ctx: dict) -> ActionResult:
        token, why = session_token(*self.login)
        if not token:
            return ActionResult(ok=False, data={}, error=why)
        try:
            key_response = request_same_origin(
                _send,
                "POST",
                f"{base_url()}/keys",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": BILLING_KEY_NAME, "scopes": ["billing"]},
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.RequestException, LiumError) as e:
            return ActionResult(ok=False, data={}, error=f"billing key request failed: {e}")

        body = _json_object(key_response)
        if key_response.status_code >= 400:
            detail = body.get("detail") or body.get("message") or (body.get("error") or {}).get("message")
            return ActionResult(ok=False, data={}, error=str(detail or f"HTTP {key_response.status_code}"))
        if body.get("scopes") != ["billing"]:
            # never keep a key as the money key when the server widened or changed its scopes; take it back
            revoked = self._revoke(token, body.get("id"))
            return ActionResult(
                ok=False,
                data={"billing_api_key_id": body.get("id"), "revoked": revoked},
                error=f"the server minted scopes {body.get('scopes')}, not ['billing']; key {body.get('id')} "
                      + ("was revoked" if revoked else "could not be revoked — revoke it on https://lium.io"),
            )
        key = body.get("key") or body.get("api_key")
        if not key:
            return ActionResult(ok=False, data={}, error="the server returned no key")

        config.set(BILLING_KEY_OPTION, key)
        config.set(BILLING_KEY_BOUND_OPTION, rent_key_tag(config.get("api.api_key")) or "")
        return ActionResult(ok=True, data={"billing_api_key": key, "billing_api_key_id": body.get("id")})

    @staticmethod
    def _revoke(token: str, key_id) -> bool:
        if not key_id:
            return False
        try:
            response = request_same_origin(
                _send, "DELETE", f"{base_url()}/keys/{quote(str(key_id), safe='')}",
                headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_TIMEOUT,
            )
        except (requests.RequestException, LiumError):
            return False
        return response.status_code < 400
