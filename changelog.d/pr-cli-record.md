### Added
- CLI: `lium record <pod>` shows what a run ran on and what it cost: GPU, image (with digest when pinned), the node's driver, CUDA and interconnect when the container came up, start and end, close reason, and the ledger total. It works after the pod is removed when given the pod's UUID. `--format json` prints the server's record.
- SDK: `Lium.pod_record(pod_id)` returns `GET /pods/{id}/record`. A server without the route answers 404 (`LiumNotFoundError`).
