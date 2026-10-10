### Changed
- `Lium.rent` (rent by spec) and `Lium.up_cluster` send an `Idempotency-Key` header with every rent, as `Lium.up` already does, so a server that honours it can answer a repeated request with the first rental instead of a second one. The rent is still sent once: it is not resent after a lost response until the server advertises support.
