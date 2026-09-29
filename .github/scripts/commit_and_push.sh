#!/bin/bash
# Guard, commit and push one writer run. On a rejected push, never rebase and never force:
# reset to the new tip and re-run the writer from the SAME saved inputs (recorded mode),
# so the diff is always against what is actually committed and Sleeper is not re-fetched.
# If the tip already holds a newer run, the re-run writes nothing and this exits 0.
#
# Usage: commit_and_push.sh INPUTS_DIR MESSAGE_FILE SUMMARY_FILE
set -euo pipefail

INPUTS="$1"
MSG="$2"
SUMMARY="$3"
MAX_ATTEMPTS=3
DATA_PATHS=(state log runs latest.json)

if [ ! -s "$MSG" ]; then
  echo "nothing to commit (skipped run)"
  exit 0
fi
RUN_AT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_at"])' "$SUMMARY")

attempt=1
while true; do
  python3 -m nflchanges.guard --repo . --changed
  for path in "${DATA_PATHS[@]}"; do
    # log/ does not exist until a run logs its first line (a baseline logs none), and
    # `git add` fails on a pathspec that matches nothing.
    if [ -e "$path" ] || git ls-files --error-unmatch -- "$path" >/dev/null 2>&1; then
      git add -A -- "$path"
    fi
  done
  if git diff --cached --quiet; then
    echo "nothing staged"
    exit 0
  fi
  git commit -q -F "$MSG"
  if git push origin HEAD:main; then
    echo "pushed on attempt $attempt"
    exit 0
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "::error::push failed after $MAX_ATTEMPTS attempts; never force-pushing. The run is lost; the next run's detected_between spans it."
    exit 1
  fi
  attempt=$((attempt + 1))
  echo "push rejected; resetting to the new tip and re-running from the saved inputs"
  git fetch --depth 1 origin main
  git reset --hard FETCH_HEAD
  python3 -m nflchanges.writer --repo . --recorded "$INPUTS" --at "$RUN_AT" \
    --trigger retry --message-file "$MSG"
  if [ ! -s "$MSG" ]; then
    echo "a newer run is already committed; nothing to do"
    exit 0
  fi
done
