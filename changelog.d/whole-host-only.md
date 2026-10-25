### Changed
- `lium up -c N` on a node that rents whole only (listing `min_gpu_count_for_rental` null) fails before renting with "rents whole only. Request all N GPUs." instead of the API's refusal. SDK: `ExecutorInfo.whole_host_only`.
