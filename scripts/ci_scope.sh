#!/usr/bin/env bash
# What a change needs from CI, from the paths it touches: prints code= and docs=.
#   scripts/ci_scope.sh <base commit> <head commit>
# Prose is docs/, top-level Markdown, LICENSE and NOTICE. Every other path is code.
# A base that cannot be read counts as everything changed.
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
