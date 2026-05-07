"""Single source of truth for lium-miner-portal route paths.

Mirrored from ``lium-miner-portal/src/routes/``. A future portal migration
is a one-file change here.
"""

# Auth
LOGIN_FLEXIBLE = "/auth/login-flexible"
LOGOUT = "/auth/logout"
ME = "/auth/me"

# Miner
MINER_OPT_IN = "/miners/opt-in"

# Executors
EXECUTORS = "/executors"
EXECUTOR_BY_ID = "/executors/{id}"
UPDATE_PRICE = "/executors/{id}/update-price"
UPDATE_GPU = "/executors/{id}/update-gpu"
SYNC_EXECUTOR_CENTRAL_MINER = "/executors/sync-executor-central-miner"

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
    "MINER_OPT_IN",
    "SYNC_EXECUTOR_CENTRAL_MINER",
    "UPDATE_GPU",
    "UPDATE_PRICE",
]
