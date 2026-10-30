### Added
- `lium up`, `lium clusters up` and the MCP `rent_pod` tool show the API's `backup_notice` when a pod rented without a backup because the account cannot fund one yet (also in `--json` output, `Lium.up()`'s pod dict, `RentResult.backup_notice` and `Cluster.backup_notice`).
