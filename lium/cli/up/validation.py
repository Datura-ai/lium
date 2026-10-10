"""Up command validation."""

import json
import re
from typing import Optional, Tuple

_HEREDOC_RE = re.compile(r"<<-?[\"']?(\w+)[\"']?")
_REMOTE_SOURCE_RE = re.compile(r"^(https?://|git://|git@)", re.IGNORECASE)


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
    ports: int | None = None,
) -> tuple[bool, str]:
    """Validate up command inputs."""
    # Checked first: 0 is falsy, so the filter checks below would otherwise read
    # `--min-cpus 0` as "no filter given" and answer with the wrong sentence.
    if min_cpus is not None and min_cpus <= 0:
        return False, "--min-cpus must be a positive integer"
    if count is not None and count < 1:
        return False, "--count must be at least 1"
    if ports is not None and ports < 1:
        return False, "--ports must be at least 1"

    # With a node ID, -c/--count is not a filter: it is how many of that node's GPUs to rent (GPU splitting).
    if executor_id and (gpu or country or min_cpus is not None):
        return False, "Cannot use filters (--gpu, --country, --min-cpus) when specifying a node ID"

    has_filters = bool(gpu or count or country or ports) or min_cpus is not None
    if not executor_id and not has_filters:
        return False, "Must provide either NODE_ID or filters (--gpu, --count, --country, --min-cpus, --ports)"

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


def build_context_lines(dockerfile_content: str) -> list[str]:
    """The COPY/ADD lines that read local files, which a `--dockerfile` build cannot see.

    The node builds with the Dockerfile as the only file in its build context, so
    these fail there with "not found". `COPY --from=<stage>`, heredoc sources and
    remote ADD sources need no local files and are not listed.
    """
    logical: list[str] = []
    buf: list[str] = []
    heredoc_end: str | None = None
    for line in dockerfile_content.splitlines():
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        if not buf and line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith("\\"):
            buf.append(line.rstrip()[:-1])
            continue
        buf.append(line)
        joined = " ".join(buf).strip()
        buf = []
        if joined:
            logical.append(joined)
        heredoc = _HEREDOC_RE.search(joined)
        if heredoc:
            heredoc_end = heredoc.group(1)

    found: list[str] = []
    for line in logical:
        parts = line.split(maxsplit=1)
        if len(parts) < 2 or parts[0].upper() not in ("COPY", "ADD"):
            continue
        tokens = parts[1].split()
        flags = []
        while tokens and tokens[0].startswith("--"):
            flags.append(tokens.pop(0))
        if any(flag.startswith("--from=") for flag in flags):
            continue
        rest = " ".join(tokens)
        if rest.startswith("["):
            try:
                tokens = [str(t) for t in json.loads(rest)]
            except ValueError:
                pass  # not valid JSON: Docker reads it as shell form, so keep the whitespace tokens
        sources = tokens[:-1]
        if any(not src.startswith("<<") and not _REMOTE_SOURCE_RE.match(src) for src in sources):
            found.append(line)
    return found
