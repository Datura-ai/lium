### Changed
- `lium up` and `Lium.wait_ready` poll every second for the first 40 s of a pod start (then every
  2 s until 90 s, then every 10 s), so a ready pod is reported about half a second sooner on average.
