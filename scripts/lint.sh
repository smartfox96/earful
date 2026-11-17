#!/usr/bin/env bash
# The static checks: formatting, lint rules, types, and that every deliberate break in
# tests/mutants.py still applies to the code. The lint job in CI runs this.
#
#   scripts/lint.sh          check; changes nothing
#   scripts/lint.sh --fix    reformat and apply ruff's safe fixes first, then check
#
# The tools are pinned in pyproject.toml, in the `lint` dependency group. They run in an
# environment of their own, .venvs/lint: the latest supported release, which mypy needs
# to check the adapter against, with the tools added. Every check runs even if an earlier
# one fails.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

env_dir=.venvs/lint
[ -x "$env_dir/bin/python" ] || uv venv -q --python 3.13 "$env_dir"
VIRTUAL_ENV="$env_dir" uv pip install -q --exclude-newer 2026-11-14 \
  -e . "livekit-agents==1.8.12" pytest pytest-asyncio || exit 1
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
