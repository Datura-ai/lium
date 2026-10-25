"""Volumes rm parsing."""


def split_targets(indices: str) -> list[str]:
    return [t.strip() for t in indices.split(',') if t.strip()]


def parse(indices: str, volumes_data: list, live_volumes: list | None = None) -> tuple[dict, str]:
    """Resolve each target: a row number of the cached `lium volumes` list, or a volume HUID, id or name."""
    volumes_to_remove = []

    for index_str in split_targets(indices):
        if index_str.isdigit():
            idx = int(index_str)
            if idx < 1 or idx > len(volumes_data):
                return {}, f"Index {index_str} out of range (1..{len(volumes_data)})"
            volumes_to_remove.append((idx, volumes_data[idx - 1]))
            continue
        matches = [v for v in live_volumes or [] if index_str in (v.get('id'), v.get('huid'), v.get('name'))]
        if not matches:
            return {}, f"No volume matches {index_str!r}. Run 'lium volumes' to list them."
        if len(matches) > 1:
            # a shared name must never pick one volume to delete
            return {}, f"{index_str!r} matches {len(matches)} volumes. Use the HUID."
        volumes_to_remove.append((None, matches[0]))

    return {"volumes_to_remove": volumes_to_remove}, ""
