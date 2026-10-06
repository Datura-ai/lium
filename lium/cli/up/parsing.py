"""Up command parsing."""

from datetime import datetime, timezone, timedelta
from typing import Optional, TypedDict
import re


class ParseResult(TypedDict):
    termination_time: Optional[datetime]
    ttl: Optional[timedelta]
    volume_id: Optional[str]
    volume_create_params: Optional[dict[str, str]]


def parse(ttl: Optional[str], until: Optional[str], volume: Optional[str]) -> tuple[ParseResult | dict, str]:
    """Parse all up command inputs.

    ``--ttl`` stays a duration (``ttl``); the command turns it into a time at the rent, after the
    node lookup and the confirmation prompt, so the pod gets the whole duration. ``--until`` is
    already a point in time (``termination_time``).
    """
    result: ParseResult = {
        "termination_time": None,
        "ttl": None,
        "volume_id": None,
        "volume_create_params": None
    }

    if ttl:
        duration, error = parse_duration(ttl)
        if error:
            return {}, error
        result["ttl"] = duration

    if until:
        termination_time, error = parse_time_spec(until)
        if error:
            return {}, error
        result["termination_time"] = termination_time

    if volume:
        volume_id, volume_create_params, error = parse_volume_spec(volume)
        if error:
            return {}, error
        result["volume_id"] = volume_id
        result["volume_create_params"] = volume_create_params

    return result, ""


def parse_duration(duration_str: str) -> tuple[timedelta | None, str]:
    """Parse duration string like '6h', '45m', '2d'."""
    duration_str = duration_str.strip().lower()
    match = re.match(r'^(\d+(?:\.\d+)?)(m|h|d)$', duration_str)

    if not match:
        return None, f"Invalid TTL format: '{duration_str}'. Use format like '6h', '45m', '2d'"

    value = float(match.group(1))
    unit = match.group(2)

    if unit == 'm':
        return timedelta(minutes=value), ""
    elif unit == 'h':
        return timedelta(hours=value), ""
    elif unit == 'd':
        return timedelta(days=value), ""

    return None, "Invalid duration unit"


def parse_time_spec(time_spec: str) -> tuple[datetime | None, str]:
    """Parse time specification like 'today 23:00', 'tomorrow 01:00'."""
    import time

    time_spec = time_spec.strip().lower()
    now_local = datetime.now()
    utc_offset = timedelta(seconds=-time.timezone if not time.daylight else -time.altzone)

    try:
        if time_spec.startswith('today '):
            time_part = time_spec[6:].strip()
            if ':' in time_part:
                parts = time_part.split(':')
                hour = int(parts[0])
                minute = int(parts[1])
                second = int(parts[2]) if len(parts) > 2 else 0
                target = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)
                if target <= now_local:
                    return None, "Termination time must be in the future"
                return target.replace(tzinfo=timezone.utc) - utc_offset, ""

        elif time_spec.startswith('tomorrow '):
            time_part = time_spec[9:].strip()
            if ':' in time_part:
                parts = time_part.split(':')
                hour = int(parts[0])
                minute = int(parts[1])
                second = int(parts[2]) if len(parts) > 2 else 0
                target = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)
                target += timedelta(days=1)
                return target.replace(tzinfo=timezone.utc) - utc_offset, ""

        elif ' ' in time_spec:
            target = datetime.strptime(time_spec, "%Y-%m-%d %H:%M")
            return target.replace(tzinfo=timezone.utc) - utc_offset, ""

        elif '-' in time_spec and len(time_spec) == 10:
            target = datetime.strptime(time_spec, "%Y-%m-%d")
            return target.replace(tzinfo=timezone.utc) - utc_offset, ""

    except (ValueError, IndexError):
        pass

    return None, "Invalid or past termination time"


def parse_volume_spec(volume: str) -> tuple[str | None, dict | None, str]:
    """Parse volume specification.

    Returns (volume_id, create_params, error_message).
    """
    from lium.cli.utils import resolve_volume_huid

    volume = volume.strip()

    if volume.startswith('id:'):
        huid = volume[3:].strip()
        if not huid:
            return None, None, "Volume HUID is missing after 'id:'"

        volume_id = resolve_volume_huid(huid)
        if not volume_id:
            return None, None, f"Volume with HUID '{huid}' not found. Run 'lium volumes' first."

        return volume_id, None, ""

    elif volume.startswith('new'):
        create_params = {'name': '', 'description': ''}

        if len(volume) > 3:
            if volume[3] != ':':
                return None, None, f"Invalid format: expected 'new' or 'new:name=...' but got '{volume}'"

            params_str = volume[4:].strip()
            if params_str:
                for param in params_str.split(','):
                    param = param.strip()
                    if '=' not in param:
                        return None, None, f"Invalid parameter format: '{param}'. Expected 'key=value'"

                    key, value = param.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"')

                    if key == 'name':
                        create_params['name'] = value
                    elif key == 'desc':
                        create_params['description'] = value
                    else:
                        return None, None, f"Unknown parameter: '{key}'. Use 'name' or 'desc'"

        if not create_params['name']:
            return None, None, "Volume 'new:' requires 'name' parameter"

        return None, create_params, ""

    return None, None, f"Invalid volume spec: {volume}"
