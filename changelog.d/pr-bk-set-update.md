### Fixed
- `lium bk set` on a pod that already has a backup schedule now updates it in place (`PUT /backup-configs/{id}`) instead of deleting it and creating a new one; a schedule the server refuses leaves the old one running, where before the pod was left with no backups at all.
- `lium bk set` checks the schedule before sending it: `--every` 1h to 168h (7d), `--keep` 1d to 365d, no `..` in `--path`, and the whole value must match (`--every 1d2h` was read as 24h). A refused value exits 2 with nothing sent.

### Added
- SDK: `Lium.backup_update(config_id, *, path, frequency_hours, retention_days)`.
