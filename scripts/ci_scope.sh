#!/usr/bin/env bash
# What a run of CI has to do. Prints code= and docs=, each true or false.
#   scripts/ci_scope.sh <base commit> <head commit>                  from the paths that differ
#   scripts/ci_scope.sh --event <GitHub event> [<commit before a push>]   for a run at HEAD
# Prose is docs/, top-level Markdown, LICENSE and NOTICE. Every other path is code.
# A base that cannot be read counts as everything changed.
set -euo pipefail

say() {
  printf 'code=%s\ndocs=%s\n' "$1" "$2"
  exit 0
}

if [ "${1:-}" = "--event" ]; then
  case "${2:?the event is missing}" in
    pull_request) set -- HEAD^1 HEAD ;; # HEAD is the merge into the base
    push)
      if [ "$("$(dirname "$0")/ci_tested.sh" "$(git rev-parse HEAD)")" = "tested=true" ]; then
        echo "These files passed ci as a pull request: nothing to run again." >&2
        say false false
      fi
      set -- "${3:-}" HEAD
      ;;
    *) say true true ;; # nightly and manual runs check everything
  esac
fi

base="${1:-}"
head="${2:-HEAD}"
git cat-file -e "$base^{commit}" 2>/dev/null || say true true

# Captured first: a diff that fails must fail the script. A rename counts under both names.
paths="$(git diff --no-renames --name-only "$base" "$head")"

code=false
docs=false
while IFS= read -r path; do
  case "$path" in
    "") ;;
    docs/*) docs=true ;;
    */*) code=true ;;
    *.md | LICENSE | NOTICE) ;;
    *) code=true ;;
  esac
done <<<"$paths"

say "$code" "$docs"
