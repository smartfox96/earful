#!/usr/bin/env bash
# Is there a release of a framework on PyPI that its extra in pyproject.toml does not admit?
#   scripts/newer_release.sh
# Asks uv twice for each framework, with no date: for the newest release its extra admits,
# and for the newest there is. Exits 1 and names the release if the two differ. Needs uv.
set -euo pipefail
cd "$(dirname "$0")/.."

# The release of package $1 that uv resolves the remaining arguments to.
resolved() {
  local package="$1" lines
  shift
  lines="$(uv pip compile -q --no-deps --no-header --no-annotate --python-version 3.13 "$@")"
  sed -n "s/^$package==//p" <<<"$lines"
}

found=0
for pair in livekit=livekit-agents pipecat=pipecat-ai; do
  extra="${pair%%=*}"
  package="${pair#*=}"
  admitted="$(resolved "$package" --extra "$extra" pyproject.toml)"
  newest="$(resolved "$package" - <<<"$package")"
  if [ -z "$admitted" ] || [ -z "$newest" ]; then
    echo "uv did not say which $package is the newest: admitted '$admitted', on PyPI '$newest'" >&2
    exit 2
  fi
  if [ "$admitted" = "$newest" ]; then
    echo "$package $newest is the newest release, and earful[$extra] admits it"
  else
    echo "$package $newest is on PyPI, and earful[$extra] admits nothing after $admitted" >&2
    found=1
  fi
done
exit "$found"
