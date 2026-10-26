### Fixed
- `lium mine` stops at "Validating node" with Docker's error when pulling the preflight image fails, instead of
  running an older cached image that can refuse newly supported GPUs.
