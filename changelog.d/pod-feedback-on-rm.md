### Added
- `lium rm POD -y --rating 1-5 --feedback "..."` and `Lium().down(pod, feedback=..., rating=...)` tell
  Lium how the pod's node went in the same call that removes it; `Lium().pod_feedback(pod, ...)` sends
  feedback alone. The feedback goes in the removal call itself, is recorded once per pod (a retried
  removal does not rate the node twice) and never blocks the removal.
