#!/usr/bin/env bash
# Build one pinned environment per livekit-agents release.
#
#   scripts/make_env.sh <livekit-agents version> <resolve-as-of date, YYYY-MM-DD>
#
# The date matters. Old livekit-agents releases import packages they never declared
# (1.6.5 imports httpx, which only arrived through openai's dependencies at the time).
# Resolving "as of" a few days after the release reproduces the dependency set the
# release was actually tested with; resolving against today's index does not import.
set -euo pipefail

version="$1"
as_of="$2"
root="$(cd "$(dirname "$0")/.." && pwd)"
env_dir="$root/.venvs/lk-$version"

rm -rf "$env_dir"
uv venv -q --python "${EARFUL_PYTHON:-3.13}" "$env_dir"
VIRTUAL_ENV="$env_dir" uv pip install -q --exclude-newer "$as_of" \
  "livekit-agents==$version" pytest pytest-asyncio
VIRTUAL_ENV="$env_dir" uv pip install -q --no-deps -e "$root"
VIRTUAL_ENV="$env_dir" uv pip freeze --exclude-editable > "$root/compat/lk-$version.lock.txt"

"$env_dir/bin/python" -c 'import livekit.agents as a, livekit.rtc as r; print("livekit-agents", a.__version__, "| livekit", r.__version__)'
