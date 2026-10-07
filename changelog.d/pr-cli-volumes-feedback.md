### Fixed
- `lium volumes new` prints the new volume's HUID and the `--volume id:` line to attach it.
- `lium volumes` with no volumes says so and clears the saved list, so `volumes rm 1` can't hit a volume no longer shown.
- `lium volumes rm` names each volume in the prompt and on success, refuses a list older than 10 minutes, and gives each failure's reason; `lium schedules rm` gives the reason too.
- `lium up --volume id:<HUID>` finds a volume made in the web app without running `lium volumes` first.
