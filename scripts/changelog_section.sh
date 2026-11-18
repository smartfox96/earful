#!/usr/bin/env bash
# Print one version's section of the changelog, without its heading: a release's notes.
#   scripts/changelog_section.sh <version> [<changelog>]
# Exits 1 if the changelog has no section for the version, or an empty one.
set -euo pipefail

version="$1"
changelog="${2:-$(dirname "$0")/../CHANGELOG.md}"

# A section ends at the next version's heading, or at the link definitions that close the file.
section="$(awk -v heading="## [$version]" '
  index($0, heading) == 1 { found = 1; next }
  found && (/^## \[/ || /^\[[^]]+\]: /) { exit }
  found { print }
' "$changelog" | sed '/./,$!d')"

if [ -z "$section" ]; then
  echo "$changelog has no section for $version" >&2
  exit 1
fi
printf '%s\n' "$section"
