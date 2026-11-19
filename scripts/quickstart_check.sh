#!/usr/bin/env bash
# The README's quickstart as a newcomer runs it: from the wheel, outside the repository.
#   scripts/quickstart_check.sh               builds the wheel first
#   scripts/quickstart_check.sh <directory>   uses the wheel already in it
# EARFUL_LIVEKIT and EARFUL_AS_OF pin the framework release. Needs uv.
set -euo pipefail
export TZ=UTC # uv reads a date in the machine's time zone

root="$(cd "$(dirname "$0")/.." && pwd)"
release="${EARFUL_LIVEKIT:-1.8.12}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
began="$(date +%s)"

echo "=== install"
if [ $# -gt 0 ]; then
  dist="$(cd "$1" && pwd)"
else
  dist="$work/dist"
  uv build -q --wheel --out-dir "$dist" "$root"
fi
wheel="$(ls "$dist"/earful-*.whl)"
uv venv -q --python "${EARFUL_PYTHON:-3.13}" "$work/venv"
VIRTUAL_ENV="$work/venv" uv pip install -q --exclude-newer "${EARFUL_AS_OF:-2026-11-14}" \
  "${wheel}[livekit]" "livekit-agents==$release" pytest
installed="$(date +%s)"
echo "ok: $((installed - began)) s"

mkdir "$work/project"
cp "$root/examples/booking/agent.py" "$root/examples/booking/test_booking.py" "$work/project/"
cd "$work/project"
export PATH="$work/venv/bin:$PATH"
fail() {
  echo "$1" >&2
  exit 1
}

echo "=== 3. run it: the conversation passes, the search finds a second booking"
status=0
output="$(pytest -q 2>&1)" || status=$?
[ "$status" -eq 1 ] || fail "pytest exited $status, not 1: $output"
for wanted in "1 failed, 1 passed" "A2/rule  in " \
  "2 durable effects for 'book-table'; the scenario authorized 1" \
  "changed: backend.timeout 1" "search 1 (booking) breaks A2/rule"; do
  grep -qF -- "$wanted" <<<"$output" || fail "pytest did not print: $wanted"
done
lines="$(wc -l <<<"$output")"
[ "$lines" -lt 60 ] || fail "the failure takes $lines lines to read"
echo "ok"

echo "=== 4. look at it: the failure names a report that opens offline"
page=".earful/test_booking.py-test_every_timing_and_fault_near_it/search-1.booking.A2-rule.html"
grep -qF -- "$page" <<<"$output" || fail "the failure does not name $page"
[ -s "$page" ] || fail "$page was not written"
grep -q "<svg" "$page" || fail "$page holds no timeline"
if grep -q "://" "$page"; then fail "$page refers to something outside itself"; fi
echo "ok"

echo "=== 5. keep it: the failure is reduced, saved and replays"
status=0
earful search test_booking:booking --keep A2/rule --out failures >/dev/null || status=$?
[ "$status" -eq 1 ] || fail "earful search exited $status, not 1"
kept="failures/lk-$release.booking.A2-rule"
earful replay "$kept" --expect reproduced >/dev/null || fail "the kept failure does not replay"
echo "ok"

echo "=== 6. fix it: with an idempotency key sent, both tests pass and the kept failure is gone"
python - <<'PY'
from pathlib import Path

agent = Path("agent.py")
source = agent.read_text()
as_shipped = "await self._reservations.create(party_size)"
assert source.count(as_shipped) == 1
fixed = 'await self._reservations.create(party_size, idempotency_key=f"table-for-{party_size}")'
agent.write_text(source.replace(as_shipped, fixed))
PY
output="$(pytest -q 2>&1)" || fail "pytest still fails after the fix: $output"
grep -q "2 passed" <<<"$output" || fail "pytest did not pass both tests: $output"
earful replay "$kept" --expect clean >/dev/null || fail "the kept failure still reproduces"
echo "ok"

echo "=== after it: earful init writes a first test for an agent, and that test passes"
earful init agent:BookingAgent >/dev/null || fail "earful init refused the example's agent"
output="$(pytest -q test_booking_agent.py 2>&1)" || fail "the test earful init wrote fails: $output"
grep -q "2 passed" <<<"$output" || fail "the test earful init wrote did not pass: $output"
echo "ok"

echo "the quickstart took $(( $(date +%s) - installed )) s after a $((installed - began)) s install"
