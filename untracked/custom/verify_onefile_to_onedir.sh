#!/usr/bin/env bash
# Manually verify the onefile -> onedir self-update migration on REAL binaries.
#
# Builds the frozen 0.0.24 onefile (origin/main = what users currently have) and
# this branch's dual artifacts (onedir tarball + legacy bare onefile), serves a
# local fake release with both assets, installs the frozen binary as a managed
# install, and drives two self-update hops, printing PASS/FAIL for each:
#   HOP 1: frozen 0.0.24 onefile --(v0.0.25 dual)--> bare onefile (new code)
#   HOP 2: that binary           --(v0.0.26)------> onedir bundle
#
# Run from the repo root:  bash untracked/custom/verify_onefile_to_onedir.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
BRANCH="$(git branch --show-current)"
WT=/tmp/lium-frozen-verify

cleanup() { git worktree remove "$WT" --force >/dev/null 2>&1 || true; rm -rf "$WT"; }
trap cleanup EXIT

echo "=== [1/3] Building frozen 0.0.24 onefile (origin/main) ==="
git worktree remove "$WT" --force >/dev/null 2>&1 || true
git worktree add --detach "$WT" origin/main >/dev/null
( cd "$WT" && uv sync --frozen --extra dev >/dev/null 2>&1 && uv run pyinstaller lium.spec --clean >/dev/null 2>&1 )
FROZEN="$WT/dist/lium"
file "$FROZEN" | grep -q executable && echo "  frozen onefile: $FROZEN"

echo "=== [2/3] Building this branch's dual artifacts ($BRANCH) ==="
rm -rf dist build
bash scripts/build.sh macos >/dev/null 2>&1
ls dist/lium-darwin-arm64 dist/lium-darwin-arm64.tar.gz >/dev/null
echo "  bare onefile + onedir tarball built"

echo "=== [3/3] Driving two self-update hops on the real binaries ==="
FROZEN="$FROZEN" uv run python - <<'PY'
import os, shutil, tempfile, threading, hashlib, subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FROZEN = Path(os.environ["FROZEN"]).resolve()
BARE   = Path("dist/lium-darwin-arm64").resolve()
TGZ    = Path("dist/lium-darwin-arm64.tar.gz").resolve()

tmp = Path(tempfile.mkdtemp()).resolve()
relroot = tmp / "rel"
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()

def make_release(ver):
    d = relroot / "releases" / "download" / f"v{ver}"
    d.mkdir(parents=True)
    shutil.copy2(BARE, d / "lium-darwin-arm64")
    shutil.copy2(TGZ, d / "lium-darwin-arm64.tar.gz")
    (d / "checksums.txt").write_text(
        f"{sha(d/'lium-darwin-arm64.tar.gz')}  lium-darwin-arm64.tar.gz\n"
        f"{sha(d/'lium-darwin-arm64')}  lium-darwin-arm64\n"
    )

make_release("0.0.25")
make_release("0.0.26")

srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(relroot)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"

home = tmp / "home"
vd = home / ".lium" / "versions" / "0.0.24"
vd.mkdir(parents=True)
shutil.copy2(FROZEN, vd / "lium")
(home / ".lium" / "bin").mkdir(parents=True)
cli = home / ".lium" / "bin" / "lium"
cli.symlink_to(Path("..") / "versions" / "0.0.24" / "lium")

def update_to(ver):
    env = dict(os.environ, HOME=str(home),
               LIUM_INSTALLER_RELEASE_URL=f"{base}/releases/tag/v{ver}",
               LIUM_SELF_UPDATE_CHECK_INTERVAL_SECONDS="0",
               LIUM_INSTALLER_UNAME_S="Darwin", LIUM_INSTALLER_UNAME_M="arm64")
    subprocess.run([str(cli), "--version"], env=env, capture_output=True, text=True, timeout=120)

ok = True
update_to("0.0.25")
hop1 = os.readlink(cli) == "../versions/0.0.25/lium" and (home / ".lium/versions/0.0.25/lium").is_file()
print(f"  HOP 1 (frozen onefile -> bare onefile): {'PASS' if hop1 else 'FAIL'}  symlink={os.readlink(cli)}")
ok &= hop1

update_to("0.0.26")
hop2 = (os.readlink(cli) == "../versions/0.0.26/lium/lium"
        and (home / ".lium/versions/0.0.26/lium/lium").is_file()
        and (home / ".lium/versions/0.0.26/lium/_internal").is_dir())
print(f"  HOP 2 (new onefile -> onedir):          {'PASS' if hop2 else 'FAIL'}  symlink={os.readlink(cli)}")
ok &= hop2

srv.shutdown()
shutil.rmtree(tmp)
print("\nRESULT:", "✅ onefile -> onedir migration works" if ok else "❌ FAILED")
raise SystemExit(0 if ok else 1)
PY
