#!/usr/bin/env bash
# What an install gives today: the newest release of one framework that its extra admits,
# with every dependency as the index has it now, and the tests under a second run on it.
#   scripts/canary.sh livekit|pipecat
# Builds .venvs/canary-<framework> anew and prints what it holds. No date and no lock file:
# the set changes from day to day, and this is where that shows. Needs uv.
# EARFUL_PYTHON picks the interpreter (default 3.13).
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  livekit) package=livekit-agents ;;
  pipecat) package=pipecat-ai ;;
  *)
    echo "usage: scripts/canary.sh livekit|pipecat" >&2
    exit 2
    ;;
esac
env_dir=".venvs/canary-$1"

rm -rf "$env_dir"
uv venv -q --python "${EARFUL_PYTHON:-3.13}" "$env_dir"
VIRTUAL_ENV="$env_dir" uv pip install -q -e ".[$1]" pytest pytest-asyncio
holds="$(VIRTUAL_ENV="$env_dir" uv pip freeze -q --exclude-editable)"
echo "=== what an install holds today: $(grep "^$package==" <<<"$holds")"
echo "$holds"

echo "=== the tests under a second"
if [ "$1" = pipecat ]; then
  scripts/ran_every_test.sh "$env_dir/bin/python" tests/test_pipecat.py -m "not slow"
else
  # A release with no reference ledgers skips the tests that compare with them.
  "$env_dir/bin/python" -m pytest -q -m "not slow" -rs
fi
