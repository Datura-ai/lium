## 1.0.1

- Rent GPU Pod: required Idempotency Key, sent as the `Idempotency-Key` header, so a replayed step returns the first pod (24-hour window). A max price of 0 or an invalid one is refused instead of ignored.
- The pod trigger is now "New Pod": it fires once per pod. It no longer claims to fire on status changes.
- New GPU Offer: fires for a server first seen after the Zap is enabled; servers seen before do not fire again.
- Terminate Pod: returns `deletion_requested: true` (teardown may finish later) instead of `terminated: true`.

## 1.0.0

- Initial version: New GPU Offer and pod triggers, Find GPU Offers search, Rent GPU Pod and Terminate Pod actions.
