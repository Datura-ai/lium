## 1.0.4

- Rent GPU Pod: the pod-name code comes from the Idempotency Key and the rental (GPU model, GPU count, SSH key, template), so the same key on a different rental is no longer handed the first pod; it reaches Lium, which refuses a reused key. A replay that finds its pod returns the same fields as a fresh rental (`price_per_hour`, `template_id`, `node`).

## 1.0.2

- Rent GPU Pod: the pod name carries a code from the Idempotency Key (`zap-<12 hex>`, or your Pod Name as prefix); a replay finds that pod and returns it with `replayed: true` instead of renting, also after the 24-hour key window.
- Rent GPU Pod: a 409 for an earlier call that is still running or whose outcome was not recorded stops with a clear error telling you to check your pods before retrying.

## 1.0.1

- Rent GPU Pod: required Idempotency Key, sent as the `Idempotency-Key` header, so a replayed step returns the first pod (24-hour window). A max price of 0 or an invalid one is refused instead of ignored.
- The pod trigger is now "New Pod": it fires once per pod. It no longer claims to fire on status changes.
- New GPU Offer: fires for a server first seen after the Zap is enabled; servers seen before do not fire again.
- Terminate Pod: returns `deletion_requested: true` (teardown may finish later) instead of `terminated: true`.

## 1.0.0

- Initial version: New GPU Offer and pod triggers, Find GPU Offers search, Rent GPU Pod and Terminate Pod actions.
