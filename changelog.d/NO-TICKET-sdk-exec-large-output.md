### Fixed
- `Lium.exec` (and so `lium exec`) no longer hangs, or raises `TimeoutError` with a timeout, when a command prints more than about 2 MiB: it now reads stdout and stderr while it waits for the exit status.
