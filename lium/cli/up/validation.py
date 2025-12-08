"""Up command validation."""

from typing import Optional, Tuple


def validate(
    executor_id: str | None,
    gpu: str | None,
    count: int | None,
    country: str | None,
    ttl: str | None,
    until: str | None,
    image: str | None = None,
    template_id: str | None = None,
) -> tuple[bool, str]:
    """Validate up command inputs."""
    if executor_id and (gpu or country):
        return False, "Cannot use filters (--gpu, --country) when specifying an executor ID"

    if not executor_id and not (gpu or count or country):
        return False, "Must provide either EXECUTOR_ID or filters (--gpu, --count, --country)"

    if ttl and until:
        return False, "Cannot specify both --ttl and --until"

    if image and template_id:
        return False, "Cannot specify both --image and --template_id"

    return True, ""


def parse_env_vars(env_list: Tuple[str, ...]) -> Tuple[dict, Optional[str]]:
    """Parse environment variable arguments into a dict.

    Args:
        env_list: Tuple of 'KEY=VALUE' strings

    Returns:
        (env_dict, error_message)
    """
    env_dict = {}
    for env_str in env_list:
        if "=" not in env_str:
            return {}, f"Invalid environment variable format: '{env_str}'. Use KEY=VALUE"
        key, value = env_str.split("=", 1)
        if not key:
            return {}, f"Empty key in environment variable: '{env_str}'"
        env_dict[key] = value
    return env_dict, None
