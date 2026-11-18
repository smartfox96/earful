#!/usr/bin/env bash
# Install the Pipecat adapter and its pinned test dependencies on Python 3.13.
set -euo pipefail
cd "$(dirname "$0")/.."
env_dir=.venvs/pc-1.16.0
[ -x "$env_dir/bin/python" ] || uv venv -q --python 3.13 "$env_dir"
uv pip install -q --python "$env_dir/bin/python" -r compat/pc-1.16.0.lock.txt -e '.[pipecat]'
