"""Pack a directory as the build context of a custom-Dockerfile pod, the way ``docker build`` reads it.

``.dockerignore`` follows Docker's rules: one pattern per line, ``#`` comments, ``*``/``?``/``[...]``
within a path segment, ``**`` across segments, a leading ``!`` re-includes, the last matching line
wins, and a pattern that matches a directory excludes everything under it.
"""

from __future__ import annotations

import gzip
import io
import os
import posixpath
import re
import tarfile
from pathlib import Path
from typing import List, Optional, Tuple


def _pattern_regex(pattern: str) -> re.Pattern:
    out = ""
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if char == "*":
            out += "[^/]*"
        elif char == "?":
            out += "[^/]"
        elif char == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out += re.escape(char)
            else:
                body = pattern[i + 1 : end]
                if body.startswith(("!", "^")):
                    body = "^" + body[1:]
                out += f"[{body}]"
                i = end
        elif char == "\\" and i + 1 < len(pattern):
            i += 1
            out += re.escape(pattern[i])
        else:
            out += re.escape(char)
        i += 1
    return re.compile(out)


def read_dockerignore(context_dir: Path) -> List[Tuple[bool, re.Pattern]]:
    """The ``.dockerignore`` patterns of ``context_dir`` as (re-include, regex), in file order."""
    path = context_dir / ".dockerignore"
    if not path.is_file():
        return []
    rules = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        include = line.startswith("!")
        if include:
            line = line[1:].strip()
        line = posixpath.normpath(line.replace(os.sep, "/")).lstrip("/")
        if line in ("", "."):
            continue
        rules.append((include, _pattern_regex(line)))
    return rules


def is_ignored(rel_path: str, rules: List[Tuple[bool, re.Pattern]]) -> bool:
    """Whether ``rel_path`` (posix, relative to the context) is left out of the context."""
    parts = rel_path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    ignored = False
    for include, regex in rules:
        if any(regex.fullmatch(candidate) for candidate in candidates):
            ignored = not include
    return ignored


def pack(context_dir: Path, max_bytes: Optional[int] = None) -> bytes:
    """A gzipped tar of ``context_dir`` without what its ``.dockerignore`` leaves out.

    Raises ValueError for a symlink that points outside the context (the build could not
    follow it either) or an archive past ``max_bytes``. Owners are dropped: the build
    sees root-owned files, as with ``docker build``.
    """
    context_dir = Path(context_dir).resolve()
    rules = read_dockerignore(context_dir)
    # a directory whose contents a later `!` line may re-include is still walked
    has_includes = any(include for include, _ in rules)

    def _reset_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for root, dirs, files in os.walk(context_dir):
            rel_root = Path(root).relative_to(context_dir).as_posix()
            rel_root = "" if rel_root == "." else rel_root + "/"
            dirs.sort()
            kept_dirs = []
            for name in dirs:
                rel = rel_root + name
                ignored = is_ignored(rel, rules)
                if not ignored:
                    tar.add(os.path.join(root, name), arcname=rel, recursive=False, filter=_reset_owner)
                if os.path.islink(os.path.join(root, name)):
                    if not ignored:
                        _check_link(context_dir, root, name, rel)
                    continue
                if not ignored or has_includes:
                    kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in sorted(files):
                rel = rel_root + name
                full = os.path.join(root, name)
                if is_ignored(rel, rules):
                    continue
                if os.path.islink(full):
                    _check_link(context_dir, root, name, rel)
                elif not os.path.isfile(full):
                    continue  # sockets, fifos and devices are not build inputs
                tar.add(full, arcname=rel, recursive=False, filter=_reset_owner)
                if max_bytes is not None and buffer.tell() > max_bytes:
                    raise ValueError(
                        f"the build context is larger than {max_bytes} bytes; "
                        "list what the build does not need in .dockerignore"
                    )
    data = buffer.getvalue()
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(
            f"the build context is larger than {max_bytes} bytes; list what the build does not need in .dockerignore"
        )
    return data


def _check_link(context_dir: Path, root: str, name: str, rel: str) -> None:
    target = os.readlink(os.path.join(root, name))
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target.replace(os.sep, "/")))
    if os.path.isabs(target) or resolved == ".." or resolved.startswith("../"):
        raise ValueError(f"{rel} is a symlink to {target}, outside the build context")
