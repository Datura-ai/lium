### Fixed
- `lium up --image name@sha256:<digest>` now rents the pinned build. The CLI used to split the
  reference at the last `:`, inside the digest, and the backend refused it.
