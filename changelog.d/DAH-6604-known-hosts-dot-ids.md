### Security

- The pod ids `.` and `..` now have their own pin files in `~/.lium/known_hosts_dot_ids/`, instead of resolving to the pin directory or its parent, so they never share a pin with the pods `_` and `__` and forgetting one never drops another pod's key.
