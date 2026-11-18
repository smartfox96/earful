#!/usr/bin/env bash
# Formatting, lint, types, and that every break in tests/mutants.py still applies.
#   scripts/lint.sh          check; changes nothing
#   scripts/lint.sh --fix    reformat and apply ruff's safe fixes first
# Runs in .venvs/lint. Every check runs even if an earlier one fails.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

env_dir=.venvs/lint
[ -x "$env_dir/bin/python" ] || uv venv -q --python 3.13 "$env_dir"
VIRTUAL_ENV="$env_dir" uv pip install -q --exclude-newer 2026-11-18 \
  -r compat/pc-1.16.0.lock.txt -e . "livekit-agents==1.8.12" || exit 1
VIRTUAL_ENV="$env_dir" uv pip install -q --group lint || exit 1

if [ "${1:-}" = "--fix" ]; then
  "$env_dir/bin/ruff" format .
  "$env_dir/bin/ruff" check --fix .
fi

failed=0
step() {
  echo "=== $1"
  shift
  "$@" || failed=1
}
step "ruff format" "$env_dir/bin/ruff" format --check .
step "ruff check" "$env_dir/bin/ruff" check .
step "mypy" "$env_dir/bin/mypy"
step "mutants apply" "$env_dir/bin/python" scripts/mutate.py --verify
exit "$failed"
