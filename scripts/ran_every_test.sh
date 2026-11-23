#!/usr/bin/env bash
# Run pytest, and fail if it ran no test or skipped one.
#   scripts/ran_every_test.sh [--without <module>]... <python> <pytest arguments>
# For a job that has to run every test it names: a file that skips itself passes pytest.
# --without names a framework the environment does not have, as it is imported: livekit
# or pipecat. A test file that skips itself because that module cannot be imported is
# then left out, and any other skip still fails.
set -euo pipefail

without=""
while [ "${1:-}" = --without ]; do
  if [[ ! "${2:-}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "--without takes the name of a module, such as livekit; got '${2:-}'" >&2
    exit 2
  fi
  without="${without:+$without|}$2"
  shift 2
done

python="$1"
shift
report="$(mktemp)"
trap 'rm -f "$report"' EXIT

"$python" -m pytest -q --junitxml "$report" "$@"
"$python" - "$report" "$without" <<'PY'
import re
import sys
import xml.etree.ElementTree as ET

report, without = sys.argv[1:]
# What pytest.importorskip says in a file that skips itself for want of the module.
absent = re.compile(rf"Skipped: could not import '(?:{without})[.']") if without else None

ran = left_out = skipped = 0
for case in ET.parse(report).getroot().iter("testcase"):
    skip = case.find("skipped")
    if skip is None:
        ran += 1
    elif absent and skip.get("message") == "collection skipped" and absent.search(skip.text or ""):
        left_out += 1
    else:
        skipped += 1
if ran == 0 or skipped:
    sys.exit(f"pytest ran {ran} tests and skipped {skipped}: here every test has to run")
needs = " or ".join(without.split("|"))
print(f"all {ran} tests ran" + (f", {left_out} files need {needs}" if without else ""))
PY
