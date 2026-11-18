#!/usr/bin/env bash
# Build the pinned environment for one livekit-agents release.
#   scripts/make_env.sh <version> <resolve-as-of date, YYYY-MM-DD>
# The date reproduces what the release shipped with: old releases import packages
# they never declared. It runs to the end of that day in UTC.
# EARFUL_PYTHON picks the interpreter (default 3.13).
# EARFUL_LOCK=check compares the result with compat/lk-<version>.lock.txt, and
# EARFUL_LOCK=write records it there (scripts/env_lock.sh).
set -euo pipefail
export TZ=UTC # uv reads a date in the machine's time zone

version="$1"
as_of="$2"
root="$(cd "$(dirname "$0")/.." && pwd)"
env_dir="$root/.venvs/lk-$version"

rm -rf "$env_dir"
uv venv -q --python "${EARFUL_PYTHON:-3.13}" "$env_dir"
VIRTUAL_ENV="$env_dir" uv pip install -q --exclude-newer "$as_of" \
  -e "$root" "livekit-agents==$version" pytest pytest-asyncio
"$root/scripts/env_lock.sh" "$env_dir" "$root/compat/lk-$version.lock.txt"

"$env_dir/bin/python" -c 'import livekit.agents as a, livekit.rtc as r; print("livekit-agents", a.__version__, "| livekit", r.__version__)'
