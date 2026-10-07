### Fixed
- `Lium.stream_exec` (and so `@lium.machine` output) no longer turns a multi-byte UTF-8 character split across two reads, such as a tqdm bar's `█`, into U+FFFD replacement characters.
