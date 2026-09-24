### Fixed
- `lium provider portal login` signs in again with the hotkey when the portal answers 401. It checks a cached token with `GET /auth/me` first; an unreachable portal still leaves the cached token in use, as before.
- `lium provider config set-email` signs in with the hotkey first (the portal takes a new address only from a recent sign-in) and stores the session token the portal returns for the new address in place of the one it ended. The token is not part of the printed profile, `--json` included.
