#!/usr/bin/env bash
# The static checks and the wheel, then every determinism, historical and replay check on
# every supported release. Builds the pinned environments if they are missing. The releases are checked
# side by side; each one's output is printed whole, in order, when all have finished.
#
#   scripts/check_all.sh            compare against tests/golden/ and tests/bundles/; writes nothing there
#   scripts/check_all.sh --update   rewrite tests/golden/ and tests/bundles/ from this machine's runs, then check
#
# Update only when a change to the ledger is intended, and review the diff before committing.
set -euo pipefail
cd "$(dirname "$0")/.."

declare -a RELEASES=("1.5.6 2026-04-30" "1.5.7 2026-05-06" "1.6.5 2026-07-20" "1.6.6 2026-07-20" "1.8.12 2026-11-14")
GOLDEN=tests/golden
BUNDLES=tests/bundles
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

for entry in "${RELEASES[@]}"; do
  read -r version as_of <<<"$entry"
  [ -x ".venvs/lk-$version/bin/python" ] || scripts/make_env.sh "$version" "$as_of"
done

scripts/lint.sh
scripts/wheel_smoke.sh

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
  # The reference bundles: searched for and reduced on the oldest release, which has both
  # known defects, and replayed on every release below.
  echo "=== updating reference bundles: livekit-agents 1.5.6"
  rm -rf "$BUNDLES"
  # The search exits 1 when a rule breaks, which is what these two are kept for.
  keep() {
    .venvs/lk-1.5.6/bin/python -m earful search "$1" --faults none --no-windows --keep "$2" --out "$BUNDLES" --json || [ $? -eq 1 ]
  }
  keep earful.livekit.cases:book_then_interrupt "A2/rule" | grep -E '"(directory|distinct_hashes)"'
  keep earful.livekit.cases:speak_over_new_turn "L2/rule b" | grep -E '"(directory|distinct_hashes)"'
fi

side_by_side check

echo "=== negative control: real clock, nothing patched (must give different hashes)"
.venvs/lk-1.8.12/bin/python scripts/realtime_control.py 2 | grep -E '"(distinct_hashes|kinds_identical|all_marked_invalid)"'
