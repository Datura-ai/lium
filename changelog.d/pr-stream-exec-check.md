### Added
- `Lium.stream_exec(..., check=True)` raises `RemoteExecutionError` (with `exit_code`) once the streamed command exits non-zero, so a plain `for chunk in ...` loop learns whether the remote command succeeded. Without `check` the exit status is still only the generator's return value, which a `for` loop discards.
