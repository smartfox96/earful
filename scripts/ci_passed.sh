#!/usr/bin/env bash
# Has ci passed on this commit of main? Exits 1 if not, or if GitHub cannot be asked.
#   scripts/ci_passed.sh <commit> [owner/repo]
# Yes if the commit's files passed as a pull request (scripts/ci_tested.sh), which is when
# its run on main has nothing to run again. Yes also if a run of ci on main passed on it.
# Needs gh, GH_TOKEN with read access to Actions and pull requests, and the commit here.
set -euo pipefail

commit="$1"
repo="${2:-${GITHUB_REPOSITORY:-}}"
if [ -z "$repo" ]; then
  echo "no repository is named: pass owner/repo, or set GITHUB_REPOSITORY" >&2
  exit 1
fi

if [ "$("$(dirname "$0")/ci_tested.sh" "$commit" "$repo")" = "tested=true" ]; then
  echo "ci passed on the pull request that brought $commit to main, with the same files"
  exit 0
fi

# A pull request's run checks its merge into the base, so only runs of main itself count.
runs="$(gh api "repos/$repo/actions/workflows/ci.yml/runs?head_sha=$commit&branch=main&status=success&per_page=100" \
  --jq '.workflow_runs[].event')" || {
  echo "GitHub did not say whether ci passed on $commit" >&2
  exit 1
}
if ! grep -qxE 'push|schedule|workflow_dispatch' <<<"$runs"; then
  echo "ci has not passed on $commit. Wait for its run on main to pass, then run this job again." >&2
  exit 1
fi
echo "ci passed on $commit, in a run on main"
