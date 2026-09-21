# Fixtures — API-key scopes, budgets and per-key filters (lium-platform#630, not released)

Bodies in the shape lium-platform#630 answers (`core/api_key_scopes.py scopes_payload`, `dtos/api_key.py ApiKeyResponse`,
`dtos/billing_history.py PodStatement`): `GET /keys/scopes` as `{"scopes": [{scope, title, description, can[],
route_families[], default}], "pod_visibility": [{value, description, default}], "money_routes": [...]}`; `GET /keys` rows with
`daily_budget_usd`, `monthly_budget_usd`, `max_budget_usd`, `spent_today_usd`, `spent_month_usd`, `spent_total_usd`, `pod_visibility`, `pods_count` beside the DAH-2944
fields; and `GET /billing/statement` (`BillingStatementResponse` on main) with `api_key_id` / `api_key_name` on each pod. The
scope words are copied from that branch so the tests read what the server will say — the CLI prints whatever the server sends
and never its own copy. Ids are the workspaces fixtures' ids; amounts are made up; key strings are not secrets.

- `scopes.json` — the four scopes, `billing` last (`default: false`), the two pod-visibility values, the money routes.
- `keys.json` — two keys of the Research workspace: `agent-1` (read, rent; $20/day, $300/month, $200 total; visibility `own`; 1 pod) and `ops` (no budget; visibility `account`; 0 pods).
- `refusals.json` — `GET /keys/{id}/refusals` for `agent-1`: four `api_key_budget_refused` ledger rows (window hit, route, amount asked, budget and spend then), oldest and newest out of order so the sort is exercised; the field names follow the Conductor spec of 21 Sep 2026 (the route is not on lium-platform#630's branch yet).
- `key_created_budget.json` — `POST /keys` for `agent-1`: the row plus the secret under `key`.
- `statement.json` — `GET /billing/statement?api_key_id=<agent-1>`: one running pod and one removed pod, both rented through `agent-1`.
