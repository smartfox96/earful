#!/usr/bin/env bash
# Reproduce every Gate 0 result. Builds the pinned environments if they are missing.
set -euo pipefail
cd "$(dirname "$0")/.."

declare -a RELEASES=("1.5.6 2026-04-30" "1.5.7 2026-05-06" "1.6.5 2026-07-20" "1.6.6 2026-07-20" "1.8.12 2026-11-14")
OUT=docs/gate0/evidence

for entry in "${RELEASES[@]}"; do
  read -r version as_of <<<"$entry"
  py=".venvs/lk-$version/bin/python"
  [ -x "$py" ] || scripts/make_env.sh "$version" "$as_of"
  echo "=== livekit-agents $version"
  "$py" -m pytest -q -p no:cacheprovider | tail -1
  "$py" scripts/gate0_repeat.py --out "$OUT" | grep -E '"(distinct_hashes|identical)"'
  "$py" scripts/gate0_3702_spike.py "$OUT" | grep -E '"(durable_reservations_for_one_intent|verdict_[a-z_]+)"'
  "$py" scripts/gate0_5509_spike.py "$OUT" | grep -E '"verdict_[a-z_]+"'
done
