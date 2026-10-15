# Lium for Zapier

A Zapier integration for [Lium](https://lium.io), the GPU cloud where you rent GPU servers by the hour. Once public,
Zapier lists it in its app directory and exposes the same actions to AI agents through Zapier MCP.

Built on Lium's public REST API (`https://lium.io/api`, `X-API-KEY` header), the same API the `lium` CLI and SDK use.

| Kind    | Key                  | What it does                                                                 | API call |
|---------|----------------------|------------------------------------------------------------------------------|----------|
| Auth    | `api_key`            | Lium API key (https://lium.io/api-keys); tested with `GET /users/me`         | |
| Trigger | `new_gpu_offer`      | New GPU server available, filtered by GPU model, max $/GPU-hour, free GPUs  | `GET /public/v1/nodes` (no key) |
| Trigger | `pod_status_changed` | New pod, or a pod changes status                                             | `GET /pods` |
| Search  | `find_gpu_offers`    | Available GPU servers, cheapest first                                        | `GET /public/v1/nodes` |
| Action  | `rent_gpu_pod`       | Rent the cheapest server matching GPU model, count and max price            | `POST /executors/rent-by-spec` |
| Action  | `terminate_pod`      | Terminate a pod and stop its billing                                         | `DELETE /pods/{id}` |

## Develop

```bash
cd integrations/zapier
npm install
npm test                                  # nock tests, no network
npx zapier-platform-cli validate          # schema + Zapier integration checks
```

## Operate

The integration is registered as Zapier app 247887 (`.zapierapprc`), owned by the support@lium.io Zapier account.
The deploy key is in the agent vault, item "Lium / Zapier", field `deploy key`.

```bash
export ZAPIER_DEPLOY_KEY=...              # from the vault, never commit it
npx zapier-platform-cli push              # upload this version (private until promoted)
npx zapier-platform-cli validate          # now also lists the publishing tasks for the live app
```

## Publish (public listing and Zapier MCP)

Zapier's requirements: https://docs.zapier.com/integrations/publish/integration-publishing-requirements.
`validate` after a push lists what is still open. To get from private to public:

1. Connect a Lium account in Zapier (a real API key) and build one live Zap per trigger, search and action, each
   with at least one successful run (checks T001-T005, A001, S002).
2. At least 3 Zapier users with live Zaps using the integration (check S001): invite teammates or customers with
   the version's share link (developer.zapier.com, Manage > Sharing).
3. Give Zapier's reviewers a non-expiring Lium test account under `integration-testing@zapier.com`, with enough
   balance to rent and terminate a small GPU pod.
4. Keep at least one admin on the integration's team with an `@lium.io` email.
5. developer.zapier.com > Lium > Publishing: answer the questions, pick the version, Submit for review. Zapier answers
   within about a week; after approval the app is in Beta for 90 days, then public.
