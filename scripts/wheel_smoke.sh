#!/usr/bin/env bash
# Install the wheel alone outside the repository and check it, and check the sdist.
#   scripts/wheel_smoke.sh               builds both first
#   scripts/wheel_smoke.sh <directory>   checks the wheel and the sdist already in it
# The framework is left out on purpose: checking a ledger must work without it. Needs uv.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [ $# -gt 0 ]; then
  dist="$(cd "$1" && pwd)"
else
  dist="$work/dist"
  uv build -q --out-dir "$dist" "$root"
fi
version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$root/pyproject.toml")"
wheel="$dist/earful-$version-py3-none-any.whl"
sdist="$dist/earful-$version.tar.gz"

echo "=== the version"
if [ ! -f "$wheel" ] || [ ! -f "$sdist" ] || [ "$(ls "$dist" | wc -l)" -ne 2 ]; then
  echo "$dist should hold the wheel and the sdist of $version and nothing else; it holds:" >&2
  ls "$dist" >&2
  exit 1
fi
echo "ok: $version"

echo "=== what the wheel holds"
listing="$(python3 -m zipfile -l "$wheel")"
for wanted in earful/py.typed earful/core/schemas/earful.ledger-1.schema.json \
  earful/core/schemas/earful.case-1.schema.json earful/core/schemas/earful.bundle-1.schema.json \
  earful/cli.py earful/pytest_plugin.py earful/pipecat/harness.py earful/scripted/scenario.py; do
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

echo "=== what the source distribution holds"
sources="$(tar tzf "$sdist")"
for wanted in pyproject.toml README.md CHANGELOG.md LICENSE NOTICE src/earful/py.typed \
  src/earful/core/schemas/earful.ledger-1.schema.json tests/test_golden.py docs/testing.md \
  examples/booking/agent.py scripts/ci_scope.py .github/workflows/checks.yml \
  .github/dependabot.yml .pre-commit-config.yaml SECURITY.md; do
  grep -q "/$wanted\$" <<<"$sources" || {
    echo "the source distribution does not hold $wanted" >&2
    exit 1
  }
done
if grep -E '/(\.venvs|\.earful|__pycache__|\.git)/' <<<"$sources"; then
  echo "the source distribution holds an environment or a cache" >&2
  exit 1
fi
echo "ok"

uv venv -q --python "${EARFUL_PYTHON:-3.13}" "$work/venv"
VIRTUAL_ENV="$work/venv" uv pip install -q "$wheel" pytest

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

assert importlib.util.find_spec("pipecat") is None
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
try:
    import earful.pipecat  # noqa: F401
except ImportError as missing:
    assert 'pip install "earful[pipecat]"' in str(missing), missing
else:
    raise SystemExit("earful.pipecat imported without the framework")
assert not [name for name in sys.modules if name.split(".")[0] in ("tests", "scripts", "breaks")]
print("ok: earful", earful.__version__)
PY

echo "=== the command"
[ "$(venv/bin/earful --version)" = "earful $version" ] || {
  echo "the installed command does not say it is $version" >&2
  exit 1
}
venv/bin/earful check run.jsonl >/dev/null
status=0
venv/bin/earful check broken.jsonl >/dev/null || status=$?
[ "$status" -eq 1 ] || { echo "earful check exited $status for a broken ledger, not 1" >&2; exit 1; }
# Captured first: grep -q would close the pipe and fail the command still writing.
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
