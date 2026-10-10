### Added
- `lium rm POD -y --rating 1-5 --feedback "..."` and `Lium().down(pod, feedback=..., rating=...)` tell
  Lium how the pod's node went in the same call that removes it; `Lium().pod_feedback(pod, ...)` sends
  feedback alone. Feedback is sent first and never blocks the removal.
