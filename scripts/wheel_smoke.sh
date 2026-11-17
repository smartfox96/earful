#!/usr/bin/env bash
# Build the wheel, install it alone in a fresh environment outside the repository, and
# check what a release must not get wrong: the package imports with nothing but its own
# dependencies, the schemas and the typing marker are in it, the command and the pytest
# plugin are registered, and nothing in it reaches for this repository's tests/ or scripts/.
#
#   scripts/wheel_smoke.sh
#
# The framework is left out on purpose: importing Earful and checking a ledger must work
# without it. Needs uv.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

uv build -q --wheel --out-dir "$work/dist" "$root"
wheel="$(ls "$work"/dist/earful-*.whl)"

echo "=== what the wheel holds"
listing="$(python3 -m zipfile -l "$wheel")"
for wanted in earful/py.typed earful/core/schemas/earful.ledger-1.schema.json \
  earful/core/schemas/earful.case-1.schema.json earful/core/schemas/earful.bundle-1.schema.json \
  earful/cli.py earful/pytest_plugin.py; do
  grep -q "$wanted" <<<"$listing" || {
    echo "the wheel does not hold $wanted" >&2
    exit 1
  }
done
if grep -E '(^| )(tests|scripts)/' <<<"$listing"; then
  echo "the wheel holds files of tests/ or scripts/" >&2
  exit 1
fi
echo "ok"

uv venv -q --python "${EARFUL_PYTHON:-3.13}" "$work/venv"
VIRTUAL_ENV="$work/venv" uv pip install -q "$wheel" pytest

# A ledger to check, taken out of the repository: nothing below runs inside it.
cp "$root/tests/golden/lk-1.8.12.barge_in_mid_word.ledger.normalized.jsonl" "$work/run.jsonl"
cp "$root/tests/golden/lk-1.5.6.book_then_interrupt.ledger.normalized.jsonl" "$work/broken.jsonl"
cd "$work"

echo "=== the package, without the framework"
venv/bin/python - <<'PY'
import importlib.util
import sys
from importlib import resources

import earful
from earful import ContractFailure, Effect, Predicate, check, effect, read_ledger
from earful.core import bundle, case, schema

assert importlib.util.find_spec("livekit") is None, "the framework is installed"
assert "site-packages" in earful.__file__, earful.__file__
assert resources.files("earful").joinpath("py.typed").is_file()
for module in (schema, case, bundle):
    assert module.load_schema()["$schema"].startswith("https://json-schema.org/")

report = check(read_ledger("run.jsonl"))
assert report.valid and not report.failures
report.assert_contracts()
try:
    check(read_ledger("broken.jsonl")).assert_contracts()
except ContractFailure as failure:
    assert "A2/rule" in str(failure)
else:
    raise SystemExit("a ledger that breaks A2 passed")

try:
    import earful.livekit  # noqa: F401
except ImportError as missing:
    assert 'pip install "earful[livekit]"' in str(missing), missing
else:
    raise SystemExit("earful.livekit imported without the framework")
assert not [name for name in sys.modules if name.split(".")[0] in ("tests", "scripts", "breaks")]
print("ok: earful", earful.__version__)
PY

echo "=== the command"
venv/bin/earful --version
venv/bin/earful check run.jsonl >/dev/null
status=0
venv/bin/earful check broken.jsonl >/dev/null || status=$?
[ "$status" -eq 1 ] || { echo "earful check exited $status for a broken ledger, not 1" >&2; exit 1; }
# Output is captured before it is searched: grep -q would close the pipe on its first
# match and fail the command that was still writing.
reported="$(venv/bin/earful report run.jsonl)"
grep -q "^case       barge_in_mid_word" <<<"$reported"
status=0
venv/bin/earful run earful.livekit.cases:barge_in_mid_word 2>/dev/null || status=$?
[ "$status" -eq 2 ] || { echo "earful run exited $status without the framework, not 2" >&2; exit 1; }
venv/bin/python -m earful --version >/dev/null
echo "ok"

echo "=== the pytest plugin"
options="$(venv/bin/python -m pytest --help)"
grep -q -- "--earful-out" <<<"$options"
fixtures="$(venv/bin/python -m pytest --fixtures 2>/dev/null)"
grep -q "^earful " <<<"$fixtures" || {
  echo "pytest does not list the earful fixture" >&2
  exit 1
}
echo "ok"
