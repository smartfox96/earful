#!/usr/bin/env bash
# Build .venvs/bare: Earful as "pip install earful" gives it, with no framework, on the
# oldest Python and the oldest jsonschema the package accepts, and the test tools.
#   scripts/make_bare_env.sh
set -euo pipefail
export TZ=UTC # uv reads a date in the machine's time zone
cd "$(dirname "$0")/.."

python="$(sed -n 's/^requires-python = ">=\(.*\)"$/\1/p' pyproject.toml)"
jsonschema="$(sed -n 's/^dependencies = \["jsonschema>=\([0-9.]*\)"\]$/\1/p' pyproject.toml)"
[ -n "$python" ] && [ -n "$jsonschema" ] || { echo "pyproject.toml names no floor" >&2; exit 1; }

env_dir=.venvs/bare
rm -rf "$env_dir"
uv venv -q --python "$python" "$env_dir"
VIRTUAL_ENV="$env_dir" uv pip install -q --exclude-newer 2026-11-20 \
  -e . "jsonschema==$jsonschema" --group test
"$env_dir/bin/python" -c 'import sys, jsonschema; print("Python", sys.version.split()[0], "| jsonschema", jsonschema.__version__)'
