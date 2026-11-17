#!/usr/bin/env bash
# Has CI already passed on exactly these files?
#
#   scripts/ci_tested.sh <commit on main> [owner/repo]
#
# Prints tested=true if the commit came from a pull request whose head has the same tree
# and on which the ci workflow succeeded as a pull request. The run on main would repeat
# that one file for file, so the workflow skips it. Prints tested=false in every other
# case: no pull request, a head whose files differ (main had moved on, so the merge is a
# combination nothing has run), a run that failed or had not finished when the pull
# request was merged, or a question GitHub did not answer.
#
# Needs the gh CLI and a token that can read pull requests and workflow runs (GH_TOKEN).
set -uo pipefail

commit="$1"
repo="${2:-${GITHUB_REPOSITORY:-}}"
no() {
  echo "tested=false"
  exit 0
}
[ -n "$repo" ] || no

head="$(gh api "repos/$repo/commits/$commit/pulls" \
  --jq '[.[] | select(.merged_at != null)][0].head.sha // empty' 2>/dev/null)" || no
[ -n "$head" ] || no

head_tree="$(gh api "repos/$repo/git/commits/$head" --jq '.tree.sha' 2>/dev/null)" || no
tree="$(git rev-parse "$commit^{tree}" 2>/dev/null)" || no
[ "$head_tree" = "$tree" ] || no

passed="$(gh api "repos/$repo/actions/workflows/ci.yml/runs?head_sha=$head&event=pull_request&status=success&per_page=1" \
  --jq '.total_count' 2>/dev/null)" || no
[ "${passed:-0}" -gt 0 ] || no

echo "tested=true"
