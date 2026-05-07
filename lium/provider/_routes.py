"""Single source of truth for lium-miner-portal route paths.

Mirrored from ``lium-miner-portal/src/routes/``. A future portal migration
is a one-file change here.
"""

# Auth
LOGIN_FLEXIBLE = "/auth/login-flexible"
LOGOUT = "/auth/logout"
ME = "/auth/me"

# Provider
PROVIDER_OPT_IN = "/providers/opt-in"

# Executors
EXECUTORS = "/executors"
EXECUTOR_BY_ID = "/executors/{id}"
UPDATE_PRICE = "/executors/{id}/update-price"
UPDATE_GPU = "/executors/{id}/update-gpu"
SYNC_EXECUTOR_CENTRAL_PROVIDER = "/executors/sync-executor-central-provider"

# Validators
BEST_VALIDATOR = "/validators/best-validator/{gpu_type}/{gpu_count}"

# Collateral
COLLATERAL_ACTIONS = "/collateral-actions"


__all__ = [
    "BEST_VALIDATOR",
    "COLLATERAL_ACTIONS",
    "EXECUTORS",
    "EXECUTOR_BY_ID",
    "LOGIN_FLEXIBLE",
    "LOGOUT",
    "ME",
    "PROVIDER_OPT_IN",
    "SYNC_EXECUTOR_CENTRAL_PROVIDER",
    "UPDATE_GPU",
    "UPDATE_PRICE",
]
