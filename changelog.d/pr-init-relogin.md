### Fixed
- CLI: `lium init` next to a saved API key checks it against `/users/me`. A key the API refuses (401, or a 403 that is not a balance, budget or scope refusal — login keys expire after 365 days by default and can be revoked) is discarded and the normal login runs, instead of `API key already saved` forever. Without a terminal no browser is opened: `lium init` exits 6 with `saved_key_rejected` and leaves the key in place. An API that cannot be reached keeps the key and says the check did not get through.

### Added
- CLI: `lium init --force` discards the saved API key and logs in again, even when the key still works.
