### Changed
- `Lium.rent` (rent by spec) and `Lium.up_cluster` send an `Idempotency-Key` header with every rent, as `Lium.up` already does. On a backend that advertises `rent_idempotency` on `GET /version`, a rent whose response was lost is sent once more with the same key, and the server answers with the first rental instead of a second one. On other backends the rent is still sent only once.
