### Added
- CLI: `lium quote <node> --hours N [--gpus N] [--volume-gb N] [--backup-gb N]` (without `--gpus`, the node's free GPUs, as `lium up` rents) shows a job's compute, volume and backup cost on a node at the rates billing charges, before renting. `<node>` is a `lium ls` row, a HUID or a UUID. Egress has no amount (billed at AWS cost). `--format json` prints the API's quote. Works without an API key, like `lium ls`.
- SDK: `Lium.quote(executor, hours=, gpu_count=, volume_gb=, backup_gb=)` returns `GET /executors/{uuid}/quote`. A server without the route answers 404 (`LiumNotFoundError`).
