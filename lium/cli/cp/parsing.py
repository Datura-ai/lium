"""Parsing for `lium cp POD:PATH POD:PATH`."""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from lium.sdk import PodInfo
from lium.cli.utils import parse_targets


@dataclass(frozen=True)
class Endpoint:
    pod: PodInfo
    path: str


def split_spec(spec: str) -> Tuple[Optional[str], str]:
    """``pod:/path`` -> ``("pod", "/path")``; a spec without a pod part -> ``(None, spec)``.

    A Windows-style drive letter is not a concern here (paths are on Linux
    pods), so the first colon separates pod from path.
    """
    pod, sep, path = spec.partition(":")
    if not sep or not pod:
        return None, spec
    return pod, path or "."


def parse(
    src_spec: str, dst_spec: str, all_pods: List[PodInfo]
) -> Tuple[Optional[Tuple[Endpoint, Endpoint]], Optional[str]]:
    """Resolve both ``POD:PATH`` specs, returns ``((src, dst), error)``."""
    endpoints = []
    for label, spec in (("source", src_spec), ("destination", dst_spec)):
        pod_ref, path = split_spec(spec)
        if pod_ref is None:
            return None, (
                f"{label} '{spec}' has no pod: use POD:PATH. "
                "For copies between this machine and a pod use 'lium scp' or 'lium rsync'"
            )
        matches = parse_targets(pod_ref, all_pods)
        if not matches:
            return None, f"No pods match {label} '{pod_ref}'"
        if len(matches) > 1:
            return None, f"{label} '{pod_ref}' matches {len(matches)} pods; name exactly one"
        endpoints.append(Endpoint(pod=matches[0], path=path))
    return (endpoints[0], endpoints[1]), None
