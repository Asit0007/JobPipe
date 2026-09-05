#!/usr/bin/env bash
# Put your gitignored config into GitHub repository secrets, so a manual
# `workflow_dispatch` run of .github/workflows/pipeline.yml has something to
# work from. Without them that workflow exits 1 at its first step.
#
# You do NOT need this script, and you do not need `gh`. Repository secrets are
# a web form; this only exists because there are ten of them and facts.yaml is
# ~900 lines. With no gh installed it prints the browser steps and exits 0.
#
# Values are piped on stdin, never passed as arguments -- an argument is
# visible in `ps` to every user on the box. Nothing here prints a secret value.
set -euo pipefail
cd "$(dirname "$0")/.."

die() { echo "  ! $*" >&2; exit 1; }

[ -f config/profile.yaml ] || die "config/profile.yaml missing. Run 'make config' and fill it in."
[ -f config/facts.yaml ]   || die "config/facts.yaml missing. Run 'make config' and fill it in."

# gh is a convenience, not a requirement. Missing or unauthenticated is not an
# error -- it just means the browser is the path, so say so and stop.
by_hand() {
  cat <<'MANUAL'

  gh is not available, which is fine -- nothing here needs it.

  Open:
    https://github.com/Asit0007/JobPipe/settings/secrets/actions

  "New repository secret", once per row. Open each file, select all, paste:

    REQUIRED   PROFILE_YAML      <- config/profile.yaml
    REQUIRED   FACTS_YAML        <- config/facts.yaml
    worth it   GEMINI_API_KEY    <- the GEMINI_API_KEY line in .env

  Those three are enough to run. The other seven only add sources and
  notifications, and each is one line copied out of .env:

    TELEGRAM_BOT_TOKEN  TELEGRAM_CHAT_ID  ADZUNA_APP_ID  ADZUNA_APP_KEY
    GMAIL_ADDRESS       GMAIL_APP_PASSWORD                PII_DENY_TERMS

  Before you paste FACTS_YAML: that file names your employer, your projects and
  your metrics, and this repository is public. Repository secrets are encrypted
  and are not exposed to pull requests from forks, but you are still handing
  ~48 KB of work history to a third party. The pipeline runs locally with
  `make daily` and needs none of this.

  To run the workflow once armed: the "Run workflow" button on the Actions tab.

MANUAL
  exit 0
}

command -v gh >/dev/null 2>&1 || by_hand
gh auth status >/dev/null 2>&1 || by_hand

repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)
visibility=$(gh repo view --json visibility -q .visibility)

cat <<BANNER

  Repository:  $repo  ($visibility)

  This uploads your WORK HISTORY to GitHub as encrypted repository secrets:

    PROFILE_YAML   <- config/profile.yaml
    FACTS_YAML     <- config/facts.yaml   (employer, projects, metrics)

  Secrets are encrypted at rest and masked in logs, but anyone with admin on
  this repository can overwrite them, and any workflow that runs here can read
  them. On a PUBLIC repository that includes workflows from forks if you ever
  enable that. This is a real decision, not a formality -- the whole reason
  these two files are gitignored is that they name your employer.

BANNER

read -r -p "  Upload them? [y/N] " reply
[ "$reply" = "y" ] || [ "$reply" = "Y" ] || die "aborted, nothing uploaded"

echo
gh secret set PROFILE_YAML < config/profile.yaml && echo "  set PROFILE_YAML"
gh secret set FACTS_YAML   < config/facts.yaml   && echo "  set FACTS_YAML"

# The rest live in .env. Missing or empty ones are skipped and reported, not
# uploaded blank -- an empty secret reads as "configured" to the workflow and
# fails deeper in, which is the trap TELEGRAM_CHAT_ID already set once.
if [ -f .env ]; then
  for key in GEMINI_API_KEY PII_DENY_TERMS TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID \
             ADZUNA_APP_ID ADZUNA_APP_KEY GMAIL_ADDRESS GMAIL_APP_PASSWORD; do
    value=$(sed -n "s/^${key}=//p" .env | head -1)
    if [ -z "$value" ]; then
      echo "  skipped $key (empty or absent in .env)"
    else
      printf '%s' "$value" | gh secret set "$key" && echo "  set $key"
    fi
  done
else
  echo "  ! no .env found; set GEMINI_API_KEY and the rest by hand"
fi

cat <<'DONE'

  Done. Verify with:   gh secret list
  Run it with:         gh workflow run pipeline.yml
  Then watch:          gh run watch

  The workflow is manual only -- there is no schedule. Run it after 12:30 IST,
  when Google rolls the free-tier quota day at midnight America/Los_Angeles;
  before that you are on the previous day's remaining quota.

  It is also not the normal way to run this. The dedup database does not
  survive between runs on a runner, so an unattended schedule here eventually
  re-queues jobs you already reviewed. `make daily` locally is the default.
DONE
