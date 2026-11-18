#!/usr/bin/env bash
# Compare what an environment holds with its lock file, or write the lock file from it.
#   EARFUL_LOCK=check scripts/env_lock.sh <environment> <lock file>
#   EARFUL_LOCK=write scripts/env_lock.sh <environment> <lock file>
# Without EARFUL_LOCK it does nothing. A lock file lists every package but Earful, as
# resolved on Python 3.13: another Python resolves to another set, and is refused.
set -euo pipefail

env_dir="$1"
lock="$2"
mode="${EARFUL_LOCK:-}"
case "$mode" in
  "") exit 0 ;;
  check | write) ;;
  *)
    echo "EARFUL_LOCK is '$mode', and it can be check or write" >&2
    exit 2
    ;;
esac

python="$("$env_dir/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ "$python" != 3.13 ]; then
  echo "EARFUL_LOCK=$mode: the lock files describe Python 3.13, and $env_dir has $python" >&2
  exit 2
fi

holds="$(VIRTUAL_ENV="$env_dir" uv pip freeze -q --exclude-editable | grep -v '^earful[ =]')"
if [ "$mode" = write ]; then
  printf '%s\n' "$holds" >"$lock"
elif ! diff -u "$lock" <(printf '%s\n' "$holds"); then
  echo "$env_dir does not hold what $lock lists." >&2
  echo "If the new set is the right one, build the environment again with EARFUL_LOCK=write." >&2
  exit 1
fi
