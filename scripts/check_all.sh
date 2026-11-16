#!/usr/bin/env bash
# Every determinism and historical check, on every supported release. Builds the pinned
# environments if they are missing.
#
#   scripts/check_all.sh            compare against the ledgers in tests/golden/; writes nothing there
#   scripts/check_all.sh --update   rewrite tests/golden/ from this machine's runs, then check
#
# Update only when a change to the ledger is intended, and review the diff before committing.
set -euo pipefail
cd "$(dirname "$0")/.."

declare -a RELEASES=("1.5.6 2026-04-30" "1.5.7 2026-05-06" "1.6.5 2026-07-20" "1.6.6 2026-07-20" "1.8.12 2026-11-14")
GOLDEN=tests/golden

for entry in "${RELEASES[@]}"; do
  read -r version as_of <<<"$entry"
  [ -x ".venvs/lk-$version/bin/python" ] || scripts/make_env.sh "$version" "$as_of"
done

if [ "${1:-}" = "--update" ]; then
  # Every release first: the checks below compare each release with all committed ledgers.
  for entry in "${RELEASES[@]}"; do
    read -r version _ <<<"$entry"
    echo "=== updating reference ledgers: livekit-agents $version"
    ".venvs/lk-$version/bin/python" scripts/repeat.py --update "$GOLDEN" | grep -E '"(case|identical)"'
  done
fi

for entry in "${RELEASES[@]}"; do
  read -r version _ <<<"$entry"
  py=".venvs/lk-$version/bin/python"
  echo "=== livekit-agents $version"
  "$py" -m pytest -q -p no:cacheprovider | tail -1
  "$py" scripts/repeat.py --check "$GOLDEN" | grep -E '"(case|distinct_hashes|identical|matches_committed)"'
  "$py" scripts/spike_3702.py | grep -E '"(durable_reservations_for_one_intent|verdict_[a-z_]+)"'
  "$py" scripts/spike_5509.py | grep -E '"verdict_[a-z_]+"'
done

echo "=== negative control: real clock, nothing patched (must give different hashes)"
"$py" scripts/realtime_control.py 2 | grep -E '"(distinct_hashes|kinds_identical|all_marked_invalid)"'
