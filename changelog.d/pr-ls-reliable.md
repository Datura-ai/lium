### Added
- `lium ls --reliable` (and `Lium.ls(reliable=True)`) hides nodes whose rents often fail to start: 2 or more failed
  starts in the last 7 days, at least 30% of the node's rents. `ExecutorInfo.frequent_rent_failures` marks them.
