### Added
- CLI: `lium job run COMMAND --gpu TYPE --budget USD --deadline 30m [--input LOCAL:REMOTE] [--output REMOTE:LOCAL] [--env K=V]` runs one bounded job through `Lium.run_job` and always removes the pod. It exits 0 on success, the command's exit code when it failed, and 124 when the budget or deadline stopped it; `--format json` prints the job result.
