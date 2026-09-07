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
    dockerfile: str | None = None,
    min_cpus: int | None = None,
) -> tuple[bool, str]:
    """Validate up command inputs."""
    # Checked first: 0 is falsy, so the filter checks below would otherwise read
    # `--min-cpus 0` as "no filter given" and answer with the wrong sentence.
    if min_cpus is not None and min_cpus <= 0:
        return False, "--min-cpus must be a positive integer"
    if count is not None and count < 1:
        return False, "--count must be at least 1"

    # With a node ID, -c/--count is not a filter: it is how many of that node's GPUs to rent (GPU splitting).
    if executor_id and (gpu or country or min_cpus is not None):
        return False, "Cannot use filters (--gpu, --country, --min-cpus) when specifying a node ID"

    has_filters = bool(gpu or count or country) or min_cpus is not None
    if not executor_id and not has_filters:
        return False, "Must provide either NODE_ID or filters (--gpu, --count, --country, --min-cpus)"

    if ttl and until:
        return False, "Cannot specify both --ttl and --until"

    if image and template_id:
        return False, "Cannot specify both --image and --template_id"

    if dockerfile and image:
        return False, "Cannot specify both --dockerfile and --image"

    if dockerfile and template_id:
        return False, "Cannot specify both --dockerfile and --template_id"

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
