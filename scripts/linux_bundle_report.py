#!/usr/bin/env python3
"""Report what the Linux PyInstaller bundle ships and fail below the OpenSSL floor.

Runs in ci.yml / release.yml after the smoke test that follows `docker build -f Dockerfile.build`.
PyInstaller copies the build image's libssl.so.1.1, libcrypto.so.1.1 and libpython into
dist/lium/_internal. The bundle has no interpreter to ask, so the versions are read inside the build
image; the OpenSSL and Python rows are tied to the bundle by the sha256 of those files and say so,
the wheel rows are the build venv's, and the run fails when a bundled file is not the image's.
0.0.34 shipped OpenSSL deb11u3 (17 CVE fixes behind 0.0.33) after #150 dropped bullseye-security,
and nothing in CI looked at the artefact (#223 pinned deb11u8).

usage: linux_bundle_report.py --bundle dist/lium --image lium-build:<tag> [--name lium-linux-amd64]

Prints a markdown table (also appended to $GITHUB_STEP_SUMMARY when set) and exits 1 when
libssl1.1 is below OPENSSL_FLOOR or a bundled library differs from the image's file.
Standard library only: the runner's python3 executes it, not the project venv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# The last bullseye-security build of OpenSSL 1.1.1; Dockerfile.build installs exactly this
# package from snapshot.debian.org (#223). Anything below it is the base image's deb11u3.
OPENSSL_FLOOR = "1.1.1w-0+deb11u8"
OPENSSL_LIBS = ("libssl.so.1.1", "libcrypto.so.1.1")
PACKAGES = ("requests", "paramiko", "cryptography")
PROBE_TIMEOUT = 300  # seconds; the probe hashes three files and imports three packages, normally < 5 s

# Runs inside the build image with the venv PyInstaller collected from (/app/.venv, created by
# `uv sync` in Dockerfile.build). Prints one JSON object.
PROBE = r"""
import hashlib, importlib, json, os, ssl, subprocess, sys, sysconfig

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def run(*argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout

files = {}
for line in run("dpkg", "-L", "libssl1.1").splitlines():
    if line.endswith(("/libssl.so.1.1", "/libcrypto.so.1.1")):
        files[os.path.basename(line)] = {"path": line, "sha256": sha256(line)}
libpython = os.path.join(sysconfig.get_config_var("LIBDIR"), sysconfig.get_config_var("INSTSONAME"))
files[os.path.basename(libpython)] = {"path": libpython, "sha256": sha256(libpython)}
print(json.dumps({
    "libssl1.1": run("dpkg-query", "-W", "--showformat=${Version}", "libssl1.1"),
    "openssl": ssl.OPENSSL_VERSION,
    "python": sys.version.split()[0],
    "packages": {name: importlib.import_module(name).__version__ for name in %r},
    "files": files,
}))
""" % (PACKAGES,)


def _order(c: str) -> int:
    """dpkg's character weight for the non-digit parts of a version: ~ sorts before everything,
    letters by code point, other characters after letters, digits and end-of-string are 0."""
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    if c == "~":
        return -1
    return ord(c) + 256


def _verrevcmp(a: str, b: str) -> int:
    """dpkg's verrevcmp(): alternate runs of non-digits (by _order) and runs of digits (numerically)."""
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def debian_version_compare(a: str, b: str) -> int:
    """Compare two Debian package versions ([epoch:]upstream[-revision]) like `dpkg --compare-versions`.
    Returns < 0, 0 or > 0."""

    def split(v: str) -> tuple[int, str, str]:
        epoch, _, rest = v.partition(":") if ":" in v else ("0", "", v)
        upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
        return int(epoch), upstream, revision

    ea, ua, ra = split(a)
    eb, ub, rb = split(b)
    if ea != eb:
        return ea - eb
    return _verrevcmp(ua, ub) or _verrevcmp(ra, rb)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_image(image: str) -> dict:
    """The probe's JSON, or a SystemExit carrying the container's stderr (the probe source is not repeated)."""
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", image, "/app/.venv/bin/python", "-W", "ignore", "-c", PROBE],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"probe of {image} did not finish within {PROBE_TIMEOUT} s") from None
    if proc.returncode != 0:
        raise SystemExit(f"probe of {image} failed (exit {proc.returncode}):\n{proc.stderr.strip()}")
    try:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise SystemExit(f"probe of {image} printed no JSON:\n{proc.stdout[-500:]}") from None
    missing = [lib for lib in OPENSSL_LIBS if lib not in info["files"]]
    if missing or not any(f.startswith("libpython") for f in info["files"]):
        raise SystemExit(
            f"probe of {image} did not find {missing or ['libpython']} in the image: {sorted(info['files'])}"
        )
    return info


def openssl_text(libcrypto: Path) -> str:
    """The OPENSSL_VERSION_TEXT constant as it sits in the bundled libcrypto ("OpenSSL 1.1.1w  11 Sep 2023")."""
    m = re.search(rb"OpenSSL \d+\.\d+\.\d+[a-z]*\s+\d{1,2} [A-Z][a-z]{2} \d{4}", libcrypto.read_bytes())
    return m.group(0).decode() if m else "not found"


def report(bundle: Path, image: str, name: str) -> tuple[str, list[str]]:
    """Return (markdown table, failures)."""
    info = probe_image(image)
    internal = bundle / "_internal"
    failures: list[str] = []
    identical: dict[str, bool] = {}
    for fname, meta in info["files"].items():
        local = internal / fname
        problem = None
        if not local.is_file():
            problem = f"{local} is missing from the bundle (the image has {meta['path']})"
        elif sha256_file(local) != meta["sha256"]:
            problem = f"{local} sha256 differs from the build image's {meta['path']} — the versions above do not describe what ships"
        identical[fname] = problem is None
        if problem:
            failures.append(problem)

    def tied(*names: str) -> str:
        return "sha256 matches the build image" if all(identical[n] for n in names) else "see failures"

    libpython = next(f for f in info["files"] if f.startswith("libpython"))
    libssl = info["libssl1.1"]
    if debian_version_compare(libssl, OPENSSL_FLOOR) < 0:
        failures.append(
            f"libssl1.1 {libssl} in the build image is below the floor {OPENSSL_FLOOR}: the bundle would ship "
            "OpenSSL with missing security fixes, as 0.0.34 did (#223). Check the pin in Dockerfile.build."
        )
    libcrypto = internal / "libcrypto.so.1.1"
    rows = [
        (
            "OpenSSL, Debian package `libssl1.1`",
            libssl,
            f"`dpkg-query` in the build image; bundle `libssl.so.1.1` + `libcrypto.so.1.1` {tied(*OPENSSL_LIBS)}",
        ),
        ("OpenSSL, runtime string", info["openssl"], "`ssl.OPENSSL_VERSION` in the build venv"),
        (
            "OpenSSL, text in the bundle",
            openssl_text(libcrypto) if libcrypto.is_file() else "no libcrypto in bundle",
            "`_internal/libcrypto.so.1.1` bytes",
        ),
        ("Python", info["python"], f"`sys.version` in the build venv; bundle `{libpython}` {tied(libpython)}"),
    ]
    rows += [(pkg, ver, "import in the build venv") for pkg, ver in info["packages"].items()]
    verdict = "OK" if not failures else "FAIL"
    lines = [f"### {name}: what the Linux bundle ships", "", "| component | version | read from |", "|---|---|---|"]
    lines += [f"| {c} | `{v}` | {src} |" for c, v, src in rows]
    lines += [
        "",
        f"Floor: `libssl1.1` ≥ `{OPENSSL_FLOOR}` and bundled libraries identical to the image — **{verdict}**",
    ]
    lines += [f"- {f}" for f in failures]
    return "\n".join(lines) + "\n", failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bundle", required=True, type=Path, help="the onedir bundle, dist/lium")
    ap.add_argument("--image", required=True, help="the image `docker build -f Dockerfile.build` produced")
    ap.add_argument("--name", default="lium-linux", help="asset name for the summary heading")
    args = ap.parse_args(argv)
    text, failures = report(args.bundle, args.image, args.name)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
