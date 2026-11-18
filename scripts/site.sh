#!/usr/bin/env bash
# Build the example reports that the README links to, as a small static site.
#
#   scripts/site.sh <directory>
#
# Writes three pages into the directory:
#
#   booking.html        the failure the README's quickstart finds, as `earful report` draws it
#   livekit-3702.html   the kept failure of tests/bundles (docs/cases), drawn the same way
#   index.html          one page that says what the two are
#
# The booking report is made here, from a search of examples/booking on the newest
# supported release, so the page is what the code draws today. The pages workflow
# publishes the directory (.github/workflows/pages.yml).
set -euo pipefail
cd "$(dirname "$0")/.."

out="${1:?usage: scripts/site.sh <directory>}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"
bin="$PWD/.venvs/lk-1.8.12/bin"
[ -x "$bin/earful" ] || scripts/make_env.sh 1.8.12 2026-11-14

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The search exits 1: it finds the failure the example is about. --keep reduces it.
status=0
(cd examples/booking && "$bin/earful" search test_booking:booking --keep A2/rule --out "$work" >/dev/null) || status=$?
[ "$status" -eq 1 ] || { echo "the search of examples/booking exited $status, not 1" >&2; exit 1; }
"$bin/earful" report "$work"/lk-1.8.12.booking.A2-rule -o "$out/booking.html" >/dev/null
"$bin/earful" report tests/bundles/lk-1.5.6.book_then_interrupt.A2-rule -o "$out/livekit-3702.html" >/dev/null

cat >"$out/index.html" <<'HTML'
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Earful: example reports</title>
<style>
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 42rem; margin: 3rem auto; padding: 0 1rem; color: #1f2328; }
  a { color: #0969da; }
  li { margin: 0.75rem 0; }
  @media (prefers-color-scheme: dark) { body { background: #0d1117; color: #e6edf3; } a { color: #4493f8; } }
</style>
<h1>Earful: example reports</h1>
<p>Each page is one run of a voice agent, drawn by <code>earful report</code>: a timeline, the
verdict of each contract, and every event. Select a verdict to see the events it rests on.</p>
<ul>
  <li><a href="booking.html">A table booked twice</a>: the failure the quickstart finds. The
  first write commits after its caller has stopped waiting, and the retry commits a second.</li>
  <li><a href="livekit-3702.html">A tool result lost on interruption</a>: a known defect of
  livekit-agents 1.5.6, reduced to a small case and kept.</li>
</ul>
<p><a href="https://github.com/smartfox96/earful">github.com/smartfox96/earful</a></p>
</html>
HTML
ls "$out"
