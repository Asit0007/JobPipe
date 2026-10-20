#!/bin/bash
# The scheduled entry point for `jobpipe daily` on macOS, driven by the
# LaunchAgent in com.asitminz.jobpipe.daily.plist.example.
#
# Everything here exists because launchd is not a login shell. It gives the job
# a bare PATH, no profile, no venv and no working directory, and every one of
# those has its own failure that looks like a code bug:
#
#   PATH        Homebrew is not on launchd's default PATH, so `tectonic` is
#               invisible and the pdf stage skips silently (7.54 made it
#               tolerant, which means it fails QUIETLY). Prepended below.
#   python      CLAUDE.md section 10: the system python3 has none of the deps and
#               dies in config.py with ModuleNotFoundError: yaml. Use the venv.
#   unbuffered  A run killed mid-flight loses everything Python has not flushed.
#               That is what happened on 2026-09-05 -- the log held four lines of
#               make echo and nothing else. PYTHONUNBUFFERED is not optional.
#   cwd         so relative paths and `.env` resolve the way they do by hand.
#
# Takes the jobpipe command as an argument -- the LaunchAgent passes `daily` --
# so the plumbing can be smoke-tested for free:  deploy/run-daily.sh status
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONPATH="src"
export PYTHONUNBUFFERED=1
export LANG="${LANG:-en_US.UTF-8}"

PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily-$(date +%F).log"

# tee so a `launchctl kickstart -p` shows the run live and the file keeps it.
exec > >(tee -a "$LOG") 2>&1

# NO DEFAULT COMMAND, and that is deliberate. This used to default to `daily`
# when called with no arguments, which meant ANY LaunchServices launch of the
# bundle ran the whole pipeline: a double-click in Finder, Spotlight, or --
# measured 2026-09-10 -- merely adding the app to a Privacy pane in System
# Settings, which launches it to read its identity. That fired a full unattended
# run at 23:08 with the day's quota already spent. The schedule says `daily`;
# nothing else should be able to imply it.
if [ $# -eq 0 ]; then
  echo "usage: $(basename "$0") <jobpipe-cli-command> [args]"
  echo "  the LaunchAgent passes 'daily'. Try 'status' for a free smoke test."
  exit 2
fi
args=("$@")

echo "==============================================================="
echo "jobpipe ${args[*]}  --  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  root:   $ROOT"
echo "  python: $PY"
echo "  quota day (America/Los_Angeles): $(TZ=America/Los_Angeles date '+%F %H:%M')"
echo "==============================================================="

if [ ! -x "$PY" ]; then
  echo "! no venv at $PY -- run: make install PY=./.venv/bin/python"
  exit 1
fi

# launchd replays a missed 12:35 at the next wake, and that wake can be a
# 10-second maintenance DarkWake with no network (the screener's copy of this
# wrapper lost 2026-09-23 and 2026-09-29 that way). Only `daily` needs the
# network; `status` stays a free offline smoke test. The limit counts ATTEMPTS,
# not wall-clock time: a process frozen by sleep resumes at the next wake and
# keeps trying, so the run lands on the first real wake.
wait_for_network() {
  local url="${NET_CHECK_URL:-https://www.google.com}" tries="${NET_WAIT_TRIES:-60}" i
  for ((i = 1; i <= tries; i++)); do
    curl -sI --max-time 5 -o /dev/null "$url" && return 0
    [ "$i" -eq 1 ] && echo "-- no network yet, waiting (up to $tries tries)"
    sleep "${NET_WAIT_SLEEP:-10}"
  done
  echo "! no network after $tries tries -- nothing was run"
  return 1
}
if [ "${args[0]}" = "daily" ]; then
  wait_for_network || exit 1
fi

# The profile refresh (CLAUDE.md section 2) rides on this firing instead of having
# its own LaunchAgent (owner, 2026-10-20). It is in the private overlay, so a
# clone without it skips this. It writes its own log and Telegram line, and its
# exit status never stops the pipeline. SKIP_PROFILE_REFRESH=1 leaves it out.
if [ "${args[0]}" = "daily" ] && [ -f "$ROOT/deploy/run-profile-refresh.sh" ] &&
  [ -z "${SKIP_PROFILE_REFRESH:-}" ]; then
  echo "-- profile refresh first"
  /bin/bash "$ROOT/deploy/run-profile-refresh.sh" </dev/null ||
    echo "! profile refresh exited $? -- the pipeline runs anyway"
  echo
fi

# A watchdog, because launchd starts nothing while the last run is still alive.
# On 2026-10-06 `daily` hung on a silent IMAP socket for five days, and every
# scheduled run behind it was skipped with no log line at all. Normal runs take
# 25-57 minutes; RUN_MAX_MINUTES (default 180) is the point where the run is
# called stuck and killed, so tomorrow's still fires. Like the network wait,
# `sleep` does not count time the Mac spends asleep.
start=$(date +%s)
# <&0: a background job otherwise gets /dev/null, and a hand-run `site` loses its prompt.
"$PY" -m jobpipe.cli "${args[@]}" <&0 &
child=$!
limit=$(( ${RUN_MAX_MINUTES:-180} * 60 ))
# Every step is chained with && to the sleep before it: when the run finishes,
# the sleep is killed, and nothing after it may run -- least of all a kill aimed
# at a PID that has finished and may since belong to another process.
(
  sleep "$limit" &&
    echo &&
    echo "! watchdog: still running after ${RUN_MAX_MINUTES:-180} min -- killing it so the next scheduled run can start" &&
    kill -TERM "$child" 2>/dev/null &&
    sleep 30 &&
    kill -KILL "$child" 2>/dev/null
) 2>/dev/null &   # its message is on stdout; stderr only carries bash's "Terminated" noise
watchdog=$!
wait "$child"
rc=$?
pkill -P "$watchdog" 2>/dev/null
wait "$watchdog" 2>/dev/null
echo
echo "--- exit $rc after $(( ($(date +%s) - start) / 60 ))m $(( ($(date +%s) - start) % 60 ))s"

# Keep a month. A log nobody prunes is a log nobody reads.
find "$LOG_DIR" -name 'daily-*.log' -type f -mtime +30 -delete 2>/dev/null

exit $rc
