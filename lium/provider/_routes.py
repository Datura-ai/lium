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
SET_EMAIL = "/auth/set-email"
SET_MACHINE_REQUEST_SUBSCRIPTION = "/auth/set-machine-request-subscription"

# Provider / miner
PROVIDER_OPT_IN = "/providers/opt-in"
MINER_OPT_IN = "/miners/opt-in"
MINERS = "/miners"

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
    "BILLING",
    "BILLING_BY_MINER",
    "ESTIMATED_REWARDS",
    "EXECUTOR_BY_ID",
    "EXECUTOR_MACHINE_ADDED",
    "EXECUTOR_MACHINE_REQUESTS",
    "EXECUTOR_MIN_GPU_FOR_RENTAL",
    "EXECUTOR_NOTICE_PERIOD",
    "EXECUTOR_PODS",
    "EXECUTORS",
    "LOGIN_FLEXIBLE",
    "LOGOUT",
    "MACHINE_REQUEST_BY_ID",
    "MACHINE_REQUESTS",
    "MACHINES",
    "ME",
    "MINER_OPT_IN",
    "MINERS",
    "PROVIDER_OPT_IN",
    "SET_EMAIL",
    "SET_MACHINE_REQUEST_SUBSCRIPTION",
    "SYNC_EXECUTOR_CENTRAL_MINER",
    "SYNC_EXECUTOR_CENTRAL_PROVIDER",
    "SYNC_EXECUTOR_MINER_PORTAL",
    "UPDATE_GPU",
    "UPDATE_PRICE",
]
