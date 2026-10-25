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
        match = next(
            (v for v in live_volumes or [] if index_str in (v.get('id'), v.get('huid'), v.get('name'))),
            None,
        )
        if match is None:
            return {}, f"No volume matches {index_str!r}. Run 'lium volumes' to list them."
        volumes_to_remove.append((None, match))

    return {"volumes_to_remove": volumes_to_remove}, ""
