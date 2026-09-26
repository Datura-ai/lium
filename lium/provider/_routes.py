"""Single source of truth for lium-miner-portal route paths.

Mirrored from ``lium-miner-portal/src/routes/``. A future portal migration
is a one-file change here.

Collateral / reclaim / switch-validator routes are intentionally absent:
the operator workflow that backs them was retired (collateral lifecycle
moved out of the portal, validators are no longer hot-swapped per
executor). Re-add only when a corresponding portal route is re-enabled.
"""

# Auth
LOGIN_FLEXIBLE = "/auth/login-flexible"
LOGOUT = "/auth/logout"
ME = "/auth/me"
DISCORD_OAUTH_URL = "/auth/me/discord/oauth-url"
SET_EMAIL = "/auth/set-email"
SET_MACHINE_REQUEST_SUBSCRIPTION = "/auth/set-machine-request-subscription"
SET_PASSWORD = "/auth/set-password"

# Provider / miner
PROVIDER_OPT_IN = "/providers/opt-in"
MINER_OPT_IN = "/miners/opt-in"
MINERS = "/miners"
MINERS_OVERVIEW = "/miners/overview"

# Executors -- collection
EXECUTORS = "/executors"
SYNC_EXECUTOR_MINER_PORTAL = "/executors/sync-executor-miner-portal"
SYNC_EXECUTOR_CENTRAL_MINER = "/executors/sync-executor-central-miner"
# Backward-compat alias for older snapshots that used "central-provider".
SYNC_EXECUTOR_CENTRAL_PROVIDER = "/executors/sync-executor-central-provider"

# Executors -- per-executor
EXECUTOR_BY_ID = "/executors/{id}"
UPDATE_PRICE = "/executors/{id}/update-price"
UPDATE_GPU = "/executors/{id}/update-gpu"
EXECUTOR_PODS = "/executors/{id}/pods"
EXECUTOR_NOTICE_PERIOD = "/executors/{id}/notice-period"
EXECUTOR_MACHINE_ADDED = "/executors/{id}/machine-added"
EXECUTOR_MACHINE_REQUESTS = "/executors/{id}/machine-requests"
EXECUTOR_MIN_GPU_FOR_RENTAL = "/executors/{id}/min-gpu-count-for-rental"
EXECUTOR_VERIFICATION = "/executors/{id}/verification"
EXECUTOR_TIER_ELIGIBILITY = "/executors/{id}/tier-change-eligibility"
EXECUTOR_UPDATE_TIER = "/executors/{id}/update-tier"
EXECUTOR_NEW_RENTALS_PAUSE = "/executors/{id}/new-rentals/pause"
EXECUTORS_LISTING = "/executors/listing"
REGISTER_TOKEN = "/executors/register-token"

# Sign-in without a hotkey, and provider API tokens (``Authorization: Bearer lpk_…``)
LOGIN_EMAIL = "/auth/login-email"
API_TOKENS = "/auth/api-tokens"
API_TOKEN_BY_ID = "/auth/api-tokens/{token_id}"

# Human handoffs: a step only a person can do (Discord OAuth, e-mail confirmation) as one URL plus a short code
HANDOFFS = "/auth/handoffs"
HANDOFF_BY_ID = "/auth/handoffs/{handoff_id}"
EMAIL_VERIFY_CODE = "/auth/me/email/verify-code"

# Earnings (public, by hotkey) and the signed-in provider's ledger
PROVIDER_EARNINGS_DAILY = "/provider-earnings/{hotkey}/daily"
PROVIDER_EMISSIONS_DAILY = "/provider-earnings/{hotkey}/emissions/daily"
PROVIDER_LEDGER_DAILY = "/provider-ledger/daily"

# Billing
BILLING = "/billing"
BILLING_BY_MINER = "/billing/{miner_hotkey}"

# Machine requests
MACHINE_REQUESTS = "/machine-requests"
MACHINE_REQUEST_BY_ID = "/machine-requests/{request_id}"

# Machines
MACHINES = "/machines"
ESTIMATED_REWARDS = "/machines/estimated-rewards"


__all__ = [
    "API_TOKEN_BY_ID",
    "API_TOKENS",
    "BILLING",
    "BILLING_BY_MINER",
    "DISCORD_OAUTH_URL",
    "EMAIL_VERIFY_CODE",
    "ESTIMATED_REWARDS",
    "EXECUTOR_BY_ID",
    "EXECUTOR_MACHINE_ADDED",
    "EXECUTOR_MACHINE_REQUESTS",
    "EXECUTOR_MIN_GPU_FOR_RENTAL",
    "EXECUTOR_NEW_RENTALS_PAUSE",
    "EXECUTOR_NOTICE_PERIOD",
    "EXECUTOR_PODS",
    "EXECUTOR_TIER_ELIGIBILITY",
    "EXECUTOR_UPDATE_TIER",
    "EXECUTOR_VERIFICATION",
    "EXECUTORS",
    "EXECUTORS_LISTING",
    "HANDOFF_BY_ID",
    "HANDOFFS",
    "LOGIN_EMAIL",
    "LOGIN_FLEXIBLE",
    "LOGOUT",
    "MACHINE_REQUEST_BY_ID",
    "MACHINE_REQUESTS",
    "MACHINES",
    "ME",
    "MINER_OPT_IN",
    "MINERS",
    "MINERS_OVERVIEW",
    "PROVIDER_OPT_IN",
    "PROVIDER_EARNINGS_DAILY",
    "PROVIDER_EMISSIONS_DAILY",
    "PROVIDER_LEDGER_DAILY",
    "REGISTER_TOKEN",
    "SET_EMAIL",
    "SET_MACHINE_REQUEST_SUBSCRIPTION",
    "SET_PASSWORD",
    "SYNC_EXECUTOR_CENTRAL_MINER",
    "SYNC_EXECUTOR_CENTRAL_PROVIDER",
    "SYNC_EXECUTOR_MINER_PORTAL",
    "UPDATE_GPU",
    "UPDATE_PRICE",
]
