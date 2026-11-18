#!/usr/bin/env bash
# Run pytest, and fail if it ran no test or skipped one.
#   scripts/ran_every_test.sh <python> <pytest arguments>
# For a job that exists to run one test file: a file that skips itself passes pytest.
set -euo pipefail

python="$1"
shift
report="$(mktemp)"
trap 'rm -f "$report"' EXIT

"$python" -m pytest -q --junitxml "$report" "$@"
"$python" - "$report" <<'PY'
import sys
import xml.etree.ElementTree as ET

suites = list(ET.parse(sys.argv[1]).getroot().iter("testsuite"))
ran = sum(int(suite.get("tests", 0)) for suite in suites)
skipped = sum(int(suite.get("skipped", 0)) for suite in suites)
if ran == 0 or skipped:
    sys.exit(f"pytest collected {ran} tests and skipped {skipped}: here every test has to run")
print(f"all {ran} tests ran")
PY
