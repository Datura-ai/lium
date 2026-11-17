### Security

- A pod id of `.` or `..` now maps to its own pin file under `~/.lium/known_hosts/` instead of the pin directory or its parent, so host-key pinning stays on for that pod.
