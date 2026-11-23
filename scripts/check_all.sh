#!/usr/bin/env bash
# Every check, on every supported release, side by side.
#   scripts/check_all.sh            check against tests/golden/ and tests/bundles/
#   scripts/check_all.sh --update   rewrite them from this machine's runs first
set -euo pipefail
export TZ=UTC # resolution dates use UTC
cd "$(dirname "$0")/.."

# The runs side by side share the processors.
cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
declare -a RELEASES=("1.5.6 2026-04-30" "1.5.7 2026-05-06" "1.6.5 2026-07-20" "1.6.6 2026-07-20" "1.8.12 2026-11-14" "1.8.13 2026-11-20")
shared=$(( cores / ${#RELEASES[@]} ))
export EARFUL_WORKERS="${EARFUL_WORKERS:-$(( shared > 1 ? shared : 1 ))}"
GOLDEN=tests/golden
BUNDLES=tests/bundles
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

project_version="$(sed -n 's/^version = "\([^"]*\)"$/\1/p' pyproject.toml)"
for entry in "${RELEASES[@]}"; do
  read -r version as_of <<<"$entry"
  py=".venvs/lk-$version/bin/python"
  if [ ! -x "$py" ]; then
    scripts/make_env.sh "$version" "$as_of"
  elif [ "$("$py" -c 'from importlib.metadata import version; print(version("earful"))')" != "$project_version" ]; then
    uv pip install --python "$py" --exclude-newer "$as_of" --no-deps --reinstall-package earful -e .
  fi
done

scripts/lint.sh
scripts/make_pipecat_env.sh
scripts/wheel_smoke.sh
scripts/quickstart_check.sh
SITE="$(mktemp -d)"
scripts/site.sh "$SITE" >/dev/null
rm -rf "$SITE"

# Run one command per release at once; fail if any of them fails.
side_by_side() {
  local step="$1" pids=() failed=0
  for entry in "${RELEASES[@]}"; do
    read -r version _ <<<"$entry"
    "$step" "$version" >"$OUT/$step.$version" 2>&1 &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do wait "$pid" || failed=1; done
  for entry in "${RELEASES[@]}"; do
    read -r version _ <<<"$entry"
    cat "$OUT/$step.$version"
  done
  return "$failed"
}

update() {
  echo "=== updating reference ledgers: livekit-agents $1"
  ".venvs/lk-$1/bin/python" scripts/repeat.py --update "$GOLDEN" | grep -E '"(case|identical)"'
}

check() {
  local py=".venvs/lk-$1/bin/python"
  echo "=== livekit-agents $1"
  "$py" -m pytest -q -p no:cacheprovider | tail -1
  "$py" scripts/repeat.py --check "$GOLDEN" | grep -E '"(case|distinct_hashes|identical|matches_committed)"'
  "$py" scripts/spike_3702.py | grep -E '"(durable_reservations_for_one_intent|verdict_[a-z_]+)"'
  "$py" scripts/spike_5509.py | grep -E '"verdict_[a-z_]+"'
  for kept in "$BUNDLES"/*/; do
    "$py" -m earful replay "$kept" --json | grep -E '"(predicate|reproduced|repeatable|identical_to_recorded|scenario_code_changed)"'
  done
}

if [ "${1:-}" = "--update" ]; then
  # Every release first: the checks below compare each release with all committed ledgers.
  side_by_side update
  echo "=== updating reference ledgers: pipecat-ai 1.16.0"
  .venvs/pc-1.16.0/bin/python scripts/repeat.py --update "$GOLDEN" | grep -E '"(case|identical)"'
  # The reference bundles are made on the oldest release, which has both known defects.
  echo "=== updating reference bundles: livekit-agents 1.5.6"
  rm -rf "$BUNDLES"
  # The search exits 1 when a rule breaks, which is what these two are kept for.
  keep() {
    .venvs/lk-1.5.6/bin/python -m earful search "$1" --faults none --no-windows --keep "$2" --out "$BUNDLES" --json || [ $? -eq 1 ]
  }
  keep earful.livekit.cases:book_then_interrupt "A2/rule" | grep -E '"(directory|distinct_hashes)"'
  keep earful.livekit.cases:speak_over_new_turn "L2/rule b" | grep -E '"(directory|distinct_hashes)"'
fi

echo "=== pipecat-ai 1.16.0"
scripts/ran_every_test.sh --without livekit .venvs/pc-1.16.0/bin/python
.venvs/pc-1.16.0/bin/python scripts/repeat.py --check "$GOLDEN" | grep -E '"(case|distinct_hashes|identical|matches_committed)"'
side_by_side check

echo "=== negative control: real clock, nothing patched (must give different hashes)"
.venvs/lk-1.8.13/bin/python scripts/realtime_control.py 2 | grep -E '"(distinct_hashes|kinds_identical|all_marked_invalid)"'
