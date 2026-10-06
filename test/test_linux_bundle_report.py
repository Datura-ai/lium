"""scripts/linux_bundle_report.py: the CI step that says what the Linux bundle ships and fails below the OpenSSL floor.

`docker` is replaced by a script on PATH that prints a canned probe, so the tests run anywhere; the bundle is a
directory with the three libraries the real one carries.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "linux_bundle_report.py"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

_spec = importlib.util.spec_from_file_location("linux_bundle_report", SCRIPT)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

LIBS = {
    "libssl.so.1.1": b"\x7fELF fake libssl",
    "libcrypto.so.1.1": b"\x7fELF fake libcrypto\x00OpenSSL 1.1.1w  11 Sep 2023\x00",
    "libpython3.12.so.1.0": b"\x7fELF fake libpython",
}
IMAGE_PATHS = {
    "libssl.so.1.1": "/usr/lib/x86_64-linux-gnu/libssl.so.1.1",
    "libcrypto.so.1.1": "/usr/lib/x86_64-linux-gnu/libcrypto.so.1.1",
    "libpython3.12.so.1.0": "/usr/local/lib/libpython3.12.so.1.0",
}


@pytest.mark.parametrize(
    ("a", "b", "sign"),
    [
        ("1.1.1w-0+deb11u3", "1.1.1w-0+deb11u8", -1),
        ("1.1.1w-0+deb11u8", "1.1.1w-0+deb11u8", 0),
        ("1.1.1w-0+deb11u10", "1.1.1w-0+deb11u8", 1),
        ("1.1.1n-0+deb11u5", "1.1.1w-0+deb11u8", -1),
        ("3.0.11-1~deb12u2", "3.0.11-1", -1),
        ("1:1.0", "2.0", 1),
        ("1.1.1w", "1.1.1w-0+deb11u8", -1),
    ],
)
def test_debian_version_compare_orders_like_dpkg(a, b, sign):
    result = report.debian_version_compare(a, b)
    assert (result > 0) - (result < 0) == sign


def make_bundle(tmp_path: Path, libs: dict = LIBS) -> Path:
    internal = tmp_path / "lium" / "_internal"
    internal.mkdir(parents=True)
    for name, data in libs.items():
        (internal / name).write_bytes(data)
    return tmp_path / "lium"


def make_probe(libssl_version: str, image_libs: dict = LIBS) -> dict:
    return {
        "libssl1.1": libssl_version,
        "openssl": "OpenSSL 1.1.1w  11 Sep 2023",
        "python": "3.12.11",
        "packages": {"requests": "2.32.5", "paramiko": "4.0.0", "cryptography": "46.0.5"},
        "files": {
            name: {"path": IMAGE_PATHS[name], "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in image_libs.items()
        },
    }


def run_report(
    tmp_path: Path, bundle: Path, probe: dict, docker_fails: bool = False
) -> tuple[subprocess.CompletedProcess, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    docker = fake_bin / "docker"
    # records its argv, then prints the canned probe; FAKE_DOCKER_FAIL=1 fails like a missing image does
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" > "$FAKE_DOCKER_ARGV"\n'
        '[ "${FAKE_DOCKER_FAIL:-0}" = 0 ] || { echo "Unable to find image \'lium-build:test\' locally" >&2; exit 125; }\n'
        'cat "$FAKE_DOCKER_PROBE"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    probe_file = tmp_path / "probe.json"
    probe_file.write_text(json.dumps(probe), encoding="utf-8")
    summary = tmp_path / "step_summary.md"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_DOCKER_PROBE": str(probe_file),
            "FAKE_DOCKER_ARGV": str(tmp_path / "docker_argv.txt"),
            "FAKE_DOCKER_FAIL": "1" if docker_fails else "0",
            "GITHUB_STEP_SUMMARY": str(summary),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--bundle",
            str(bundle),
            "--image",
            "lium-build:test",
            "--name",
            "lium-linux-amd64",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, summary


def test_report_passes_and_writes_the_step_summary_for_a_deb11u8_bundle(tmp_path: Path):
    bundle = make_bundle(tmp_path)

    result, summary = run_report(tmp_path, bundle, make_probe("1.1.1w-0+deb11u8"))

    assert result.returncode == 0, result.stderr
    table = summary.read_text(encoding="utf-8")
    assert "### lium-linux-amd64: what the Linux bundle ships" in table
    assert "| OpenSSL, Debian package `libssl1.1` | `1.1.1w-0+deb11u8` |" in table
    assert "| OpenSSL, text in the bundle | `OpenSSL 1.1.1w  11 Sep 2023` |" in table
    assert "| Python | `3.12.11` |" in table
    for pkg, ver in (("requests", "2.32.5"), ("paramiko", "4.0.0"), ("cryptography", "46.0.5")):
        assert f"| {pkg} | `{ver}` |" in table
    assert "**OK**" in table
    assert result.stdout.strip() == table.strip()
    argv = (tmp_path / "docker_argv.txt").read_text(encoding="utf-8")
    assert argv.startswith("run --rm lium-build:test /app/.venv/bin/python -W ignore -c ")
    assert "dpkg-query -W --showformat=${Version} libssl1.1" in argv.replace('"', "").replace(", ", " ")


def test_probe_source_compiles_and_names_the_packages():
    compile(report.PROBE, "<probe>", "exec")
    assert "('requests', 'paramiko', 'cryptography')" in report.PROBE


def test_report_fails_when_docker_fails(tmp_path: Path):
    bundle = make_bundle(tmp_path)

    result, _ = run_report(tmp_path, bundle, make_probe("1.1.1w-0+deb11u8"), docker_fails=True)

    assert result.returncode == 1
    assert "probe of lium-build:test failed (exit 125):" in result.stderr
    assert "Unable to find image 'lium-build:test' locally" in result.stderr


def test_probe_stops_a_hanging_docker_with_a_message(tmp_path: Path, monkeypatch):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    (fake_bin / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setattr(report, "PROBE_TIMEOUT", 0.2)

    with pytest.raises(SystemExit) as raised:
        report.probe_image("lium-build:test")

    assert "probe of lium-build:test did not finish within 0.2 s" in str(raised.value)


def test_probe_names_a_docker_that_prints_no_json(tmp_path: Path, monkeypatch):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text("#!/bin/sh\necho 'not json'\n", encoding="utf-8")
    (fake_bin / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    with pytest.raises(SystemExit) as raised:
        report.probe_image("lium-build:test")

    assert str(raised.value) == "probe of lium-build:test printed no JSON:\nnot json\n"


def test_report_fails_when_the_image_lacks_an_openssl_library(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    probe = make_probe("1.1.1w-0+deb11u8", {name: data for name, data in LIBS.items() if name != "libcrypto.so.1.1"})

    result, _ = run_report(tmp_path, bundle, probe)

    assert result.returncode == 1
    assert "probe of lium-build:test did not find ['libcrypto.so.1.1'] in the image" in result.stderr


def test_report_says_when_the_bundled_libcrypto_carries_no_version_text(tmp_path: Path):
    libs = {**LIBS, "libcrypto.so.1.1": b"\x7fELF libcrypto without the version constant"}
    bundle = make_bundle(tmp_path, libs)

    result, summary = run_report(tmp_path, bundle, make_probe("1.1.1w-0+deb11u8", libs))

    assert result.returncode == 0, result.stderr
    assert "| OpenSSL, text in the bundle | `not found` |" in summary.read_text(encoding="utf-8")


def test_report_names_the_mismatching_library_only(tmp_path: Path):
    bundle = make_bundle(tmp_path, {**LIBS, "libpython3.12.so.1.0": b"\x7fELF some other libpython"})

    result, summary = run_report(tmp_path, bundle, make_probe("1.1.1w-0+deb11u8"))

    assert result.returncode == 1
    table = summary.read_text(encoding="utf-8")
    assert "bundle `libssl.so.1.1` + `libcrypto.so.1.1` sha256 matches the build image |" in table
    assert "bundle `libpython3.12.so.1.0` see failures |" in table


def test_report_fails_below_the_openssl_floor(tmp_path: Path):
    """Negative control: the bundle 0.0.34 shipped (base image deb11u3, #150 without #223)."""
    bundle = make_bundle(tmp_path)

    result, summary = run_report(tmp_path, bundle, make_probe("1.1.1w-0+deb11u3"))

    assert result.returncode == 1
    assert "FAIL: libssl1.1 1.1.1w-0+deb11u3 in the build image is below the floor 1.1.1w-0+deb11u8" in result.stderr
    table = summary.read_text(encoding="utf-8")
    # the libraries are the image's (only the floor failed), and the row says so
    assert (
        "| OpenSSL, Debian package `libssl1.1` | `1.1.1w-0+deb11u3` | `dpkg-query` in the build image; bundle `libssl.so.1.1` + `libcrypto.so.1.1` sha256 matches the build image |"
        in table
    )
    assert "**FAIL**" in table


def test_report_fails_when_a_bundled_library_is_not_the_image_file(tmp_path: Path):
    """A deb11u8 image whose libcrypto is not what PyInstaller copied: the version would describe nothing."""
    bundle = make_bundle(tmp_path, {**LIBS, "libcrypto.so.1.1": b"\x7fELF some other libcrypto"})

    result, _ = run_report(tmp_path, bundle, make_probe("1.1.1w-0+deb11u8"))

    assert result.returncode == 1
    assert (
        "libcrypto.so.1.1 sha256 differs from the build image's /usr/lib/x86_64-linux-gnu/libcrypto.so.1.1"
        in result.stderr
    )


def test_report_fails_when_the_bundle_lacks_a_library(tmp_path: Path):
    libs = {name: data for name, data in LIBS.items() if name != "libssl.so.1.1"}
    bundle = make_bundle(tmp_path, libs)

    result, _ = run_report(tmp_path, bundle, make_probe("1.1.1w-0+deb11u8"))

    assert result.returncode == 1
    assert "libssl.so.1.1 is missing from the bundle" in result.stderr


@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
def test_linux_build_jobs_run_the_report_after_the_smoke_test(workflow):
    jobs = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))["jobs"]
    steps = jobs["build-linux-binary"]["steps"]
    runs = [step.get("run", "") for step in steps]

    smoke = next(i for i, run in enumerate(runs) if "./dist/lium/lium --version" in run)
    check = next(
        i
        for i, run in enumerate(runs)
        if "scripts/linux_bundle_report.py --bundle dist/lium --image lium-build:${GITHUB_SHA}-${{ matrix.asset_name }}"
        in run
    )
    assert check == smoke + 1
