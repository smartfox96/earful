#!/usr/bin/env bash
# Build .venvs/lint: both adapters' frameworks at their newest supported release, pytest,
# ruff and mypy. scripts/lint.sh and scripts/mutate.py run in it.
#   scripts/make_lint_env.sh
# An environment that exists is kept and brought up to date.
set -euo pipefail
export TZ=UTC # uv reads a date in the machine's time zone
cd "$(dirname "$0")/.."

env_dir=.venvs/lint
[ -x "$env_dir/bin/python" ] || uv venv -q --python 3.13 "$env_dir"
VIRTUAL_ENV="$env_dir" uv pip install -q --exclude-newer 2026-11-18 \
  -e '.[pipecat]' "pipecat-ai==1.16.0" "livekit-agents==1.8.12" pytest pytest-asyncio
VIRTUAL_ENV="$env_dir" uv pip install -q --group lint
