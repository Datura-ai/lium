# Fixtures — API-key scopes, budgets and per-key filters (lium-platform P235, not released)

Bodies in the shape the P235 contract names (the brief of 21 Sep 2026; to be re-checked against the platform draft PR's
`dtos/api_key.py` once it is open): `GET /keys/scopes` rows (`scope`, `description`, `route_families`), `GET /keys` rows with
`daily_budget_usd`, `max_budget_usd`, `spent_today_usd`, `spent_total_usd`, `pod_visibility` beside the DAH-2944 fields, and
`GET /billing/statement` (`BillingStatementResponse` on main) with `api_key_id` / `api_key_name` on each pod. The scope
descriptions here are fixture text: the CLI prints whatever the server sends and never these words. Ids are the workspaces
fixtures' ids; amounts are made up.

- `scopes.json` — the four scopes, `billing` last.
- `keys.json` — two keys of the Research workspace: `agent-1` (read, rent; $20/day, $200 total; visibility `own`) and `ops` (no budget; visibility `account`).
- `key_created_budget.json` — `POST /keys` for `agent-1`: the row plus the secret under `key`.
- `statement.json` — `GET /billing/statement?api_key_id=<agent-1>`: one running pod and one removed pod, both rented through `agent-1`.
