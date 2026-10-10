### Fixed
- `Lium.wait_template_ready` returns a template the platform can rent (`CREATED`, `UPDATED` or `VERIFY_SUCCESS`)
  instead of waiting for a verification public templates never get, which made it time out on every public template.
