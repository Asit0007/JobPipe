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

start=$(date +%s)
"$PY" -m jobpipe.cli "${args[@]}"
rc=$?
echo
echo "--- exit $rc after $(( ($(date +%s) - start) / 60 ))m $(( ($(date +%s) - start) % 60 ))s"

# Keep a month. A log nobody prunes is a log nobody reads.
find "$LOG_DIR" -name 'daily-*.log' -type f -mtime +30 -delete 2>/dev/null

exit $rc
