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
value that stays the same when Zapier replays the step, such as the trigger item's ID. Lium keeps a key for 24 hours:
a replay inside that window returns the first pod instead of renting a second one; a replay after it rents again. A
replay while the first call is still running, or after a call whose outcome was not recorded, is refused (HTTP 409);
check your pods before retrying with a new key.

## Develop

```bash
cd integrations/zapier
npm install
npm test                                  # nock tests, no network
npx zapier-platform-cli validate          # schema + Zapier integration checks
```

## Publish

For general deployment and publication guidance, see [Zapier’s publishing requirements](https://docs.zapier.com/integrations/publish/integration-publishing-requirements).
