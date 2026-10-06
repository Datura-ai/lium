"""Parsing logic for rm command."""

import re
from datetime import datetime, timedelta, timezone
from typing import List

from lium.sdk import PodInfo
from lium.cli.utils import resolve_targets

# Shared with the command layer, which maps this one error to a distinct exit code.
NO_MATCHING_PODS = "No pods match targets"


def parse_duration(duration_str: str) -> tuple[timedelta | None, str | None]:
    """Parse duration string like '6h', '45m', '2d', returns (timedelta, error_message)."""
    if not duration_str:
        return None, "Duration cannot be empty"

    duration_str = duration_str.strip().lower()

    # Extract number and unit
    match = re.match(r'^(\d+(?:\.\d+)?)(m|h|d)$', duration_str)
    if not match:
        return None, f"Invalid duration format: '{duration_str}'. Use format like '45m', '6h', '2d'"

    value = float(match.group(1))
    unit = match.group(2)

    if unit == 'm':
        return timedelta(minutes=value), None
    elif unit == 'h':
        return timedelta(hours=value), None
    elif unit == 'd':
        return timedelta(days=value), None

    return None, f"Unknown duration unit: {unit}"


def parse_time_spec(time_spec: str) -> tuple[datetime | None, str | None]:
    """Parse time spec like 'today 23:00', converts local time to UTC, returns (datetime, error_message)."""
    import time

    time_spec = time_spec.strip().lower()

    try:
        # Get current time in local timezone (naive datetime)
        now_local = datetime.now()
        # Get local timezone offset
        utc_offset = timedelta(seconds=-time.timezone if not time.daylight else -time.altzone)

        # Handle "today HH:MM" or "today HH:MM:SS"
        if time_spec.startswith('today '):
            time_part = time_spec[6:].strip()

            if ':' in time_part:
                parts = time_part.split(':')
                hour = int(parts[0])
                minute = int(parts[1])
                second = int(parts[2]) if len(parts) > 2 else 0

                target = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)

                # If time has passed today, return error
                if target <= now_local:
                    return None, f"Time '{time_spec}' has already passed today. Use 'tomorrow HH:MM' or a future time."

                # Convert to UTC by subtracting local offset
                return target.replace(tzinfo=timezone.utc) - utc_offset, None

        # Handle "tomorrow HH:MM" or "tomorrow HH:MM:SS"
        elif time_spec.startswith('tomorrow '):
            time_part = time_spec[9:].strip()

            if ':' in time_part:
                parts = time_part.split(':')
                hour = int(parts[0])
                minute = int(parts[1])
                second = int(parts[2]) if len(parts) > 2 else 0

                target = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)
                target += timedelta(days=1)

                # Convert to UTC by subtracting local offset
                return target.replace(tzinfo=timezone.utc) - utc_offset, None

        # Handle absolute datetime "YYYY-MM-DD HH:MM" or "YYYY-MM-DD HH:MM:SS"
        elif ' ' in time_spec:
            # Parse as local time
            target = datetime.strptime(time_spec, "%Y-%m-%d %H:%M")
            # Convert to UTC by subtracting local offset
            return target.replace(tzinfo=timezone.utc) - utc_offset, None

        # Handle date only "YYYY-MM-DD" (midnight local time)
        elif '-' in time_spec and len(time_spec) == 10:
            target = datetime.strptime(time_spec, "%Y-%m-%d")
            # Convert to UTC by subtracting local offset
            return target.replace(tzinfo=timezone.utc) - utc_offset, None

    except (ValueError, IndexError) as e:
        return None, f"Invalid time format: '{time_spec}'. Use 'today HH:MM', 'tomorrow HH:MM', or 'YYYY-MM-DD HH:MM'"

    return None, f"Invalid time format: '{time_spec}'. Use 'today HH:MM', 'tomorrow HH:MM', or 'YYYY-MM-DD HH:MM'"


def parse(
    targets: str | None,
    all_flag: bool,
    all_pods: List[PodInfo],
    in_duration: str | None,
    at_time: str | None,
    allow_index: bool | None = None,
) -> tuple[dict | None, str | None]:
    """Parse rm command inputs, returns (parsed_data_dict, error_message).

    ``index_matches`` in the result lists the targets that were given as `lium ps`
    row numbers, so the command can say which pod each number stood for.
    """
    # Check if pods exist
    if not all_pods:
        return None, "No active pods"

    # Parse targets
    index_matches = []
    if all_flag:
        selected_pods = all_pods
    elif targets:
        matches = resolve_targets(targets, all_pods, allow_index=allow_index)
        selected_pods = [m.pod for m in matches]
        index_matches = [m for m in matches if m.via_index]
        if not selected_pods:
            return None, f"{NO_MATCHING_PODS}: {targets}"
    else:
        # No targets specified - will need interactive selection in command
        return None, "No targets specified"

    result = {
        "selected_pods": selected_pods,
        "index_matches": index_matches,
        "termination_time": None,
    }

    # Parse duration if provided
    if in_duration:
        duration, error = parse_duration(in_duration)
        if error:
            return None, error
        result["termination_time"] = datetime.now(timezone.utc) + duration

    # Parse time spec if provided
    if at_time:
        termination_time, error = parse_time_spec(at_time)
        if error:
            return None, error

        # Validate it's in the future
        if termination_time <= datetime.now(timezone.utc):
            return None, "Removal time must be in the future"

        result["termination_time"] = termination_time

    return result, None
