### Fixed
- CLI: `lium mine` checks for Docker's `sysbox-runc` runtime at the prerequisite step and stops with the official command that installs it, instead of starting the node and failing at validation. The CLI only prints the command; it never runs it.
