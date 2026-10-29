### Fixed
- `lium ls` showed V100 nodes as `GPU` and `lium ls --gpu V100` found none: names ending in "GPU" ("NVIDIA Tesla V100 Tensor Core GPU") now resolve to `V100` (also P100, T4, M40, K80).
