### Security

- The pod ids `.` and `..` now have their own pin files in `~/.lium/known_hosts_dot_ids/`, instead of resolving to the pin directory or its parent, so they never share a pin with the pods `_` and `__` and forgetting one never drops another pod's key.
- While `~/.lium/known_hosts` is not a directory (an older release could leave pod `.`'s pin there as a file), every SSH connection refuses to start and says to move it away, instead of connecting without being able to save the host key.
