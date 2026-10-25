### Changed
- `lium up -c N` on a node that rents whole only (listing `min_gpu_count_for_rental` null) fails before renting with "rents whole only. Request all N GPUs." when N is below the free count; otherwise no count is sent and the API rents the whole host. SDK: `ExecutorInfo.whole_host_only`.
