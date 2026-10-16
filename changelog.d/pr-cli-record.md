### Added
- CLI: `lium record <pod>` shows what a run ran on and what it cost: GPU, image (with digest when pinned), the node's driver, CUDA and interconnect when the container came up, start and end, close reason, and the ledger total. It works after the pod is removed when given the pod's UUID. `--format json` prints the server's record.
- SDK: `Lium.pod_record(pod_id)` returns `GET /pods/{id}/record`. On a server that lists `run_record` on `GET /version`, a missing pod raises `LiumNotFoundError`; a server without the route raises `LiumError` explaining that run records are unavailable.
