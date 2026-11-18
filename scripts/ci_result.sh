#!/usr/bin/env bash
# Did every job of the ci workflow do what the scope asked of it? Exits 1 if not.
#   CODE=true DOCS=false MUTANTS=true RESULTS="changes=success docs=skipped ..." scripts/ci_result.sh
# CODE, DOCS and MUTANTS are what scripts/ci_scope.sh printed, RESULTS each job's result.
# A job the scope asks for must have passed. No job may have failed or been cancelled.
set -euo pipefail

JOBS="changes docs lint wheel quickstart pipecat determinism mutants"

failed=0
wrong() {
  echo "$1" >&2
  failed=1
}

for name in CODE DOCS MUTANTS; do
  case "${!name:-}" in
    true | false) ;;
    *) wrong "the scope gave $name as '${!name:-}', which is neither true nor false" ;;
  esac
done
[ "$failed" -eq 0 ] || exit 1

seen=" "
for pair in ${RESULTS:-}; do
  job="${pair%%=*}"
  got="${pair#*=}"
  case "$job" in
    changes) due=true ;;
    docs) if [ "$CODE" = false ]; then due="$DOCS"; else due=false; fi ;;
    lint | wheel | quickstart | pipecat | determinism) due="$CODE" ;;
    mutants) due="$MUTANTS" ;;
    *)
      wrong "no rule here says when the job '$job' has to run"
      continue
      ;;
  esac
  seen="$seen$job "
  if [ "$due" = true ]; then
    [ "$got" = success ] || wrong "$job had to run and its result is '$got'"
  else
    case "$got" in
      skipped | success) ;;
      *) wrong "$job has the result '$got'" ;;
    esac
  fi
done

for job in $JOBS; do
  case "$seen" in
    *" $job "*) ;;
    *) wrong "the result of the job '$job' is missing" ;;
  esac
done

[ "$failed" -eq 0 ] && echo "every job did what the scope asked: $RESULTS"
exit "$failed"
