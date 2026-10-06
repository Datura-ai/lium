#!/usr/bin/env bash
# run.sh — the live e2e as one command: install this checkout → the CLI journey → the SDK journey → artifacts.
#
#   E2E_API_URL / E2E_API_KEY  the target (default staging.lium.io; a funded account's key — CI: secret LIUM_E2E_API_KEY)
#   E2E_MAX_PRICE              cap on $/h for the node rented (default 0.50)
#   SUITES                     default "renter sdk keys_scopes" (test_<suite>_journey.py); T_INSTALL / T_SUITE timeouts (5m / 25m)
#
# Like lium-platform's and lium-io's gate.sh: GNU timeout (or macOS gtimeout) on every step, every suite runs even after a failure,
# artifacts/ always holds timings.txt, summary.md, <suite>-junit.xml and commands.json (every CLI call, exit code,
# duration, head of its output — the key never appears in any of them). Exit 0 only when every step passed.
set -uo pipefail
cd "$(dirname "$0")"
# GNU timeout: `timeout` on Linux, `gtimeout` from Homebrew coreutils on macOS; without either the steps run unbounded
# (said once, so a Mac without coreutils still gets a run and knows why it had no time limit).
if command -v timeout >/dev/null 2>&1; then TIMEOUT=timeout
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT=gtimeout
else TIMEOUT=""; echo "e2e: no GNU timeout on PATH (macOS: brew install coreutils) — steps run without a time limit" >&2; fi
A=artifacts; mkdir -p "$A"; : > "$A/timings.txt"; rm -f "$A"/*-junit.xml "$A/summary.md" "$A/commands.json"
SUITES=${SUITES:-renter sdk keys_scopes}
T_INSTALL=${T_INSTALL:-5m}; T_SUITE=${T_SUITE:-25m}
FAILED=""
export E2E_ARTIFACTS="$PWD/$A"

step() {  # step <name> <timeout> <command...>
  local name=$1 t=$2; shift 2
  local t0=$SECONDS rc status
  echo "::group::$name"
  if [ -n "$TIMEOUT" ]; then "$TIMEOUT" -k 30 "$t" "$@"; else "$@"; fi; rc=$?
  echo "::endgroup::"
  case $rc in 0) status=pass ;; 124|137) status="TIMEOUT(>$t)" ;; *) status="FAIL(rc=$rc)" ;; esac
  printf '%s\t%ds\t%s\n' "$name" "$((SECONDS - t0))" "$status" >> "$A/timings.txt"
  echo "e2e: $name $status in $((SECONDS - t0))s"
  [ $rc -eq 0 ] || FAILED="$FAILED $name"
  return $rc
}

summary() {
  python3 - "$A" "$FAILED" "${E2E_API_URL:-https://staging.lium.io/api}" <<'PY'
import glob, os, sys
import xml.etree.ElementTree as ET
a, failed, target = sys.argv[1], sys.argv[2].split(), sys.argv[3]
rows, red = {}, []
for f in sorted(glob.glob(f"{a}/*-junit.xml")):
    suite = os.path.basename(f).removesuffix("-junit.xml")
    root = ET.parse(f).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    n = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
    for tc in root.iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None:
            red.append(f"{suite}: {tc.get('name', '')}")
    rows[suite] = f"{n['tests'] - n['failures'] - n['errors'] - n['skipped']} passed, {n['failures'] + n['errors']} failed, {n['skipped']} skipped"
out = ["| step | result | time |", "|---|---|---|"]
for line in open(f"{a}/timings.txt").read().splitlines():
    name, secs, status = line.split("\t")
    detail = rows.get(name.removeprefix("test-"), "")
    out.append(f"| {name} | {'✅' if status == 'pass' else '❌'} {status}{' — ' + detail if detail else ''} | {secs} |")
if red:
    out += ["", "Failed tests:", *[f"- `{r}`" for r in red[:20]]]
verdict = "**live e2e: PASS**" if not failed else f"**live e2e: FAIL** ({', '.join(failed)})"
open(f"{a}/summary.md", "w").write("\n".join([verdict, f"target: `{target}`", "", *out, ""]))
print("\n".join([verdict, *out]))
PY
}

if [ -z "${E2E_LIUM:-}" ]; then
  # this checkout's CLI + SDK into e2e/.venv (install.sh: uv if present, else pip); the suites run that binary
  step install "$T_INSTALL" bash install.sh || { summary; exit 1; }
  export E2E_LIUM="$PWD/.venv/bin/lium"; PY="$PWD/.venv/bin/python"
else
  PY="${PY:-python3}"
fi
for s in $SUITES; do
  step "test-$s" "$T_SUITE" "$PY" -m pytest "test_${s}_journey.py" --junitxml="$A/$s-junit.xml" || true
done
summary
[ -z "$FAILED" ]
