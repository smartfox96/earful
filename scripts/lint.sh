#!/usr/bin/env bash
# Formatting, lint, types, and that every break in tests/mutants.py still applies.
#   scripts/lint.sh          check; changes nothing
#   scripts/lint.sh --fix    reformat and apply ruff's safe fixes first
# Runs in .venvs/lint (scripts/make_lint_env.sh). Every check runs even if an earlier one fails.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

env_dir=.venvs/lint
scripts/make_lint_env.sh || exit 1

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
