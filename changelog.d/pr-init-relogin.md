### Fixed
- CLI: `lium init` next to a saved API key checks it against `/users/me`. When the API rejects it (401, or a 403 saying the key's workspace is gone or its creator left it — login keys expire after 365 days by default and can be revoked) the normal login runs and replaces it once it succeeds, instead of `API key already saved` forever; an aborted login leaves the old key in place. Any other 403 (a blocked account, a firewall page) keeps the key with a warning. Without a terminal no browser is opened: `lium init` exits 6 with `saved_key_rejected` and leaves the key in place. An API that cannot be reached keeps the key and says the check did not get through.

### Added
- CLI: `lium init --force` logs in again even when the saved API key still works; the key is replaced only once the new login succeeds. `lium init --session <ID>` now exchanges the session even next to a saved key.

### Changed
- CLI: `~/.lium/config.ini` is written to a temporary file and renamed over the old one, so an interrupted write cannot leave a truncated file.
