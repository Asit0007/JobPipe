.PHONY: help install config doctor verify ingest fetch-jd score prepare tex pdf rescreen site claims notify track review status test all gmail-auth gmail-imap-check telegram-check

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

prepare:     ## tailor resume + screening answers for shortlisted jobs
	$(CLI) prepare

tex:         ## rebuild .tex from prepared docs (no LLM call) -- `make tex JOB=56`
	$(CLI) tex $(or $(JOB),all)

pdf:         ## compile prepared .tex to PDF -- `make pdf JOB=56`
	$(CLI) pdf $(or $(JOB),all)

site:        ## export the queue as a static read-only site for Vercel/Pages
	$(CLI) site

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

all: ingest score prepare notify   ## the full daily run
