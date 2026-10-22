# Lium for Zapier

A Zapier integration for [Lium](https://lium.io), the GPU cloud where you rent GPU servers by the hour. Once public,
Zapier lists it in its app directory and exposes the same actions to AI agents through Zapier MCP.

Built on Lium's public REST API (`https://lium.io/api`, `X-API-KEY` header), the same API the `lium` CLI and SDK use.

| Kind    | Key                  | What it does                                                                 | API call |
|---------|----------------------|------------------------------------------------------------------------------|----------|
| Auth    | `api_key`            | Lium API key (https://lium.io/api-keys); tested with `GET /users/me`         | |
| Trigger | `new_gpu_offer`      | Matching GPU server first observed after the Zap is enabled (filters: GPU model, max $/GPU-hour, free GPUs). Servers present at activation, or seen before, do not trigger | `GET /public/v1/nodes` (no key) |
| Trigger | `pod_status_changed` | New Pod: once per pod you rent (status changes do not trigger)              | `GET /pods` |
| Search  | `find_gpu_offers`    | Available GPU servers, cheapest first                                        | `GET /public/v1/nodes` |
| Action  | `rent_gpu_pod`       | Rent the cheapest server matching GPU model, count and max price (> 0), with a required idempotency key | `POST /executors/rent-by-spec` |
| Action  | `terminate_pod`      | Request deletion of a pod (`deletion_requested: true`); teardown may finish asynchronously | `DELETE /pods/{id}` |

**Rent GPU Pod idempotency.** The required Idempotency Key field is sent as the `Idempotency-Key` header. Map a
value that stays the same when Zapier replays the step, such as the trigger item's ID; use a different key for each pod
(e.g. `{{id}}-2` for a second Rent step). The pod is named `<Pod Name or zap>-<12 hex>`, the hex derived from the key,
GPU model, GPU count, SSH key and template. Before renting, the action looks for one of your pods with that name and
returns it (`replayed: true`, with the same fields as a fresh rental) instead of renting again, which covers a replay
at any age while that pod still exists. The same key on a different rental does not match and reaches Lium, which
refuses a reused key within 24 hours.
Lium also keeps the key for 24 hours, so a replay in that window returns the first pod even if the listing missed it. Zapier's automatic replays all land within that window; a manual replay more than 24 hours later, after the pod was renamed or deleted, rents a new pod.
If an earlier call with the key is still running, or its outcome was not recorded, Lium answers 409 and the step stops
with an error asking you to check your pods before retrying.

## Develop

```bash
cd integrations/zapier
npm install
npm test                                  # nock tests, no network
npx zapier-platform-cli validate          # schema + Zapier integration checks
```

## Publish

For general deployment and publication guidance, see [Zapier’s publishing requirements](https://docs.zapier.com/integrations/publish/integration-publishing-requirements).
