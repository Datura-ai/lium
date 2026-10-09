### Fixed
- `lium up -c N` without a NODE_ID (and without `--gpu` on a backend that picks for it) now picks a node with N free GPUs. It matched the host's GPU total, while the rent takes only the free GPUs: on a half-rented 4-GPU host `-c 4` rented and billed 2 GPUs, then failed `gpu_count_mismatch`.
- `lium up` with filters no longer rewrites the row numbers `lium ls` stored, so a later `lium up <row>` rents the node that row showed.
