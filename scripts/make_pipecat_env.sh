#!/usr/bin/env bash
# Build the pinned environment for one pipecat-ai release, without LiveKit.
#   scripts/make_pipecat_env.sh [<version> [<resolve-as-of date, YYYY-MM-DD>]]
# With no arguments: pipecat-ai 1.16.0 as of 2026-11-18, in .venvs/pc-1.16.0.
# The date runs to the end of that day in UTC. An environment that exists is kept and
# brought up to date.
# EARFUL_PYTHON picks the interpreter of a new environment (default 3.13).
# EARFUL_LOCK=check compares the result with compat/pc-<version>.lock.txt, and
# EARFUL_LOCK=write records it there (scripts/env_lock.sh).
set -euo pipefail
export TZ=UTC # uv reads a date in the machine's time zone
cd "$(dirname "$0")/.."

version="${1:-1.16.0}"
as_of="${2:-2026-11-18}"
env_dir=".venvs/pc-$version"

[ -x "$env_dir/bin/python" ] || uv venv -q --python "${EARFUL_PYTHON:-3.13}" "$env_dir"
VIRTUAL_ENV="$env_dir" uv pip install -q --exclude-newer "$as_of" \
  -e '.[pipecat]' "pipecat-ai==$version" pytest pytest-asyncio
scripts/env_lock.sh "$env_dir" "compat/pc-$version.lock.txt"

"$env_dir/bin/python" -c 'from importlib.metadata import version; print("pipecat-ai", version("pipecat-ai"))'
