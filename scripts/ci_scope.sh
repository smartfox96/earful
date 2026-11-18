#!/usr/bin/env bash
# What a run of CI has to do. Prints code=, docs= and mutants=, each true or false.
#   scripts/ci_scope.sh <base commit> <head commit>                  from the paths that differ
#   scripts/ci_scope.sh --event <GitHub event> [<commit before a push>]   for a run at HEAD
# docs: docs/ or top-level Markdown changed, so the tests that read them run.
# code: any other path changed, so every job runs.
# mutants: src/, tests/ or the mutation run itself changed, so the deliberate breaks run.
# A base that cannot be read counts as everything changed.
set -euo pipefail

say() {
  printf 'code=%s\ndocs=%s\nmutants=%s\n' "$1" "$2" "$3"
  exit 0
}

if [ "${1:-}" = "--event" ]; then
  case "${2:?the event is missing}" in
    pull_request) set -- HEAD^1 HEAD ;; # HEAD is the merge into the base
    push)
      if [ "$("$(dirname "$0")/ci_tested.sh" "$(git rev-parse HEAD)")" = "tested=true" ]; then
        echo "These files passed ci as a pull request: nothing to run again." >&2
        say false false false
      fi
      set -- "${3:-}" HEAD
      ;;
    schedule) say true true false ;; # the mutation workflow has a nightly run of its own
    *) say true true true ;;
  esac
fi

base="${1:-}"
head="${2:-HEAD}"
git cat-file -e "$base^{commit}" 2>/dev/null || say true true true

# Captured first: a diff that fails must fail the script. A rename counts under both names.
paths="$(git diff --no-renames --name-only "$base" "$head")"

code=false
docs=false
mutants=false
while IFS= read -r path; do
  case "$path" in
    "") ;;
    docs/*) docs=true ;;
    src/* | tests/* | scripts/mutate.py | scripts/make_lint_env.sh | .github/workflows/mutation.yml)
      code=true
      mutants=true
      ;;
    */*) code=true ;;
    *.md) docs=true ;;
    *) code=true ;;
  esac
done <<<"$paths"

say "$code" "$docs" "$mutants"
