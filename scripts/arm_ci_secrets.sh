#!/usr/bin/env bash
# Arm the GitHub Actions fallback scheduler.
#
# .github/workflows/pipeline.yml needs your gitignored config as repository
# secrets. Until they exist the workflow exits 1 at its first step, which is
# why every scheduled run since it was written has failed without ever
# reaching the pipeline.
#
# Values are piped on stdin, never passed as arguments -- an argument is
# visible in `ps` to every user on the box. Nothing here prints a secret.
set -euo pipefail
cd "$(dirname "$0")/.."

die() { echo "  ! $*" >&2; exit 1; }

command -v gh >/dev/null 2>&1 || die "gh is not installed. brew install gh"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated. Run: gh auth login"

[ -f config/profile.yaml ] || die "config/profile.yaml missing. Run 'make config' and fill it in."
[ -f config/facts.yaml ]   || die "config/facts.yaml missing. Run 'make config' and fill it in."

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
  Test it now with:    gh workflow run pipeline.yml
  Then watch:          gh run watch

  The scheduler runs 08:30 UTC / 14:00 IST on weekdays -- deliberately after
  Google rolls the free-tier quota day at midnight America/Los_Angeles.
DONE
