### Fixed
- CLI: `lium mine` checks for Docker's `sysbox-runc` runtime at the prerequisite step and stops with the command that installs it, instead of starting the node and failing at validation. The printed command fetches the official installer. On a terminal it first offers to run that installer for you (one yes/no question), but only when the installer in the checkout is the official one.
