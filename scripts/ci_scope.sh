#!/usr/bin/env bash
# What a change needs from CI, decided from the paths it touches.
#
#   scripts/ci_scope.sh <base commit> <head commit>
#
# Prints two lines, in the form GitHub Actions reads as step outputs:
#
#   code=true    a file other than prose changed: run the lint job and the whole matrix
#   docs=true    a page under docs/ changed: tests/test_docs.py reads those pages
#
# Prose is docs/, a Markdown file at the top level, LICENSE and NOTICE. Every other path
# is code, the workflow and this script included. A base that cannot be read (a first
# push, a force push) counts as everything changed.
set -euo pipefail

base="${1:-}"
head="${2:-HEAD}"

if [ -z "$base" ] || ! git cat-file -e "$base^{commit}" 2>/dev/null; then
  printf 'code=true\ndocs=true\n'
  exit 0
fi

code=false
docs=false
while IFS= read -r path; do
  case "$path" in
    docs/*) docs=true ;;
    */*) code=true ;;
    *.md | LICENSE | NOTICE) ;;
    *) code=true ;;
  esac
done < <(git diff --name-only "$base" "$head")

printf 'code=%s\ndocs=%s\n' "$code" "$docs"
