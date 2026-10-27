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
| Action  | `terminate_pod`      | Request deletion of a pod (`deletion_requested: true`); teardown may finish asynchronously | `DELETE /pods/{id}` |

Renting is not offered as an action: Zapier does not publish integrations that make financial transactions. Rent pods
on lium.io, the `lium` CLI or the API.

## Develop

```bash
cd integrations/zapier
npm install
npm test                                  # nock tests, no network
npx zapier-platform-cli validate          # schema + Zapier integration checks
```

## Publish

For general deployment and publication guidance, see [Zapier’s publishing requirements](https://docs.zapier.com/integrations/publish/integration-publishing-requirements).
