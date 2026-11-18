#!/usr/bin/env bash
# Has CI already passed on exactly these files? Prints tested=true or tested=false.
#   scripts/ci_tested.sh <commit on main> [owner/repo]
# True only for a commit from a pull request with the same tree, on which ci passed.
# Needs gh and GH_TOKEN.
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
