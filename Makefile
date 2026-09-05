.PHONY: help install config doctor verify ingest fetch-jd score daily prepare tex pdf rescreen site deploy verify-deploy readme-stats claims notify track review status test all gmail-auth gmail-imap-check telegram-check

PY ?= python3
CLI := $(PY) -m jobpipe.cli
export PYTHONPATH := src

help:        ## show this help
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

install:     ## install dependencies
	pip install -r requirements.txt

config:      ## create your private config from the templates (never overwrites)
	@cp -n .env.example .env 2>/dev/null && echo "  created .env" || echo "  .env exists, left alone"
	@cp -n config/profile.example.yaml config/profile.yaml 2>/dev/null \
	  && echo "  created config/profile.yaml" || echo "  config/profile.yaml exists, left alone"
	@cp -n config/facts.example.yaml config/facts.yaml 2>/dev/null \
	  && echo "  created config/facts.yaml" || echo "  config/facts.yaml exists, left alone"
	@echo "\n  Next: put your key in .env, then run 'make doctor'."

doctor:      ## preflight: key, tier, models, config, integrations
	$(PY) scripts/doctor.py

verify:      ## ping every configured ATS slug, report dead ones
	$(CLI) verify-sources

ingest:      ## pull from all sources -> normalize -> dedup -> DB
	$(CLI) ingest

fetch-jd:    ## fill in descriptions for postings that arrived without one
	$(CLI) fetch-jd

score:       ## keyword prefilter, then LLM score on survivors
	$(CLI) score

daily:       ## THE ONE COMMAND: ingest -> score -> prepare -> pdf -> notify -> track
	# Sizes prepare to the tailor model's remaining budget and falls back to
	# flash-lite if that model is 503ing, which is the difference between this
	# and running the six stages by hand. Quota day turns at 12:30 IST.
	$(CLI) daily

prepare:     ## tailor resume + screening answers for shortlisted jobs
	$(CLI) prepare

tex:         ## rebuild .tex from prepared docs (no LLM call) -- `make tex JOB=56`
	$(CLI) tex $(or $(JOB),all)

pdf:         ## compile .tex to PDF, skipping what is current -- `make pdf JOB=56 FORCE=--force`
	# Only recompiles a .tex newer than its .pdf. `daily` runs this every night
	# over every prepared document; without the check that was 45 tectonic runs
	# to reproduce ~29 byte-identical files and reset every PDF's mtime.
	$(CLI) pdf $(or $(JOB),all) $(FORCE)

readme-stats: ## regenerate the README funnel table from the live DB (free)
	$(CLI) readme-stats

site:        ## export the queue as an encrypted static site for Vercel/Pages
	$(CLI) site

deploy:      ## re-export and push to Vercel in one step
	# The payload.enc test is belt and braces on top of cli site's own exit
	# code: nothing reaches a public host unless ciphertext is on disk.
	$(CLI) site
	@test -s site/payload.enc || { echo "site/payload.enc missing -- refusing to deploy"; exit 2; }
	# --yes is LOAD-BEARING, not a convenience. Without a TTY the CLI skips its
	# setup prompt and uploads NOTHING, while still creating a deployment that
	# reports "Ready". Measured 2026-08-31: four consecutive production
	# deployments, all Ready, all with no file tree, all serving 404 -- while
	# site/ sat on disk with a 220 kB payload.enc. This was already known and
	# written down; the fix had only ever been typed by hand in a shell, never
	# put in the Makefile, so `make deploy` kept reproducing it.
	cd site && vercel --prod --yes
	# A deploy that uploaded nothing is indistinguishable from a good one until
	# you open the site. Assert it, the same way the payload.enc test above
	# asserts the export: this project's whole bug history is "reported
	# success, produced nothing".
	@$(MAKE) --no-print-directory verify-deploy

verify-deploy: ## assert the live site actually serves the encrypted payload
	@url=$${JOBPIPE_SITE_URL:-https://jobpipe-ten.vercel.app}; \
	code=$$(curl -s -o /dev/null -w '%{http_code}' -m 30 "$$url/payload.enc"); \
	size=$$(curl -s -o /dev/null -w '%{size_download}' -m 30 "$$url/payload.enc"); \
	if [ "$$code" != "200" ]; then \
	  echo "DEPLOY FAILED: $$url/payload.enc returned $$code, not 200."; \
	  echo "  A 404 here with files on disk means the upload was empty."; \
	  exit 3; \
	elif [ "$$size" -lt 10000 ]; then \
	  echo "DEPLOY FAILED: payload.enc served only $$size bytes."; exit 3; \
	else \
	  echo "deploy verified: $$url/payload.enc 200, $$size bytes"; \
	fi

rescreen:    ## refresh screening answers after a facts.yaml change (1 call/job)
	$(CLI) rescreen $(or $(JOB),all)

claims:      ## show what the never_claim gate matches on
	$(CLI) claims

notify:      ## push today's review queue to Telegram
	$(CLI) notify

track:       ## parse Gmail for replies, update statuses, flag follow-ups
	$(CLI) track

status:      ## pipeline counts + remaining LLM budget
	$(CLI) status

gmail-auth:  ## one-time Gmail OAuth consent, then verify the token works
	$(PY) scripts/gmail_auth.py

gmail-imap-check: ## verify the Gmail App Password and the alert query (no OAuth)
	$(PY) scripts/gmail_imap_check.py

telegram-check: ## verify TELEGRAM_BOT_TOKEN/CHAT_ID actually deliver a message
	$(PY) scripts/telegram_check.py

review:      ## local review dashboard on 127.0.0.1:8080
	# Loopback only. Compose already does this; the Makefile used to expose
	# your review queue -- and your work history -- to the whole LAN.
	$(PY) -m uvicorn jobpipe.review_api:app --host 127.0.0.1 --port 8080

test:        ## run the test suite
	$(PY) -m pytest tests/ -q

# Kept because the README and muscle memory both know this name. It used to be
# `ingest score prepare notify`, which had no budget sizing, no tailor fallback
# and -- the reason ten PDFs went missing on 2026-09-01 -- no pdf stage.
all: daily   ## alias for `make daily`
