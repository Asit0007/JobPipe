.PHONY: install doctor ingest score prepare notify track review verify all

install:
	pip install -r requirements.txt

doctor:      ## preflight: key, tier, models, config, integrations
	python scripts/doctor.py

verify:      ## ping every configured ATS slug, report dead ones
	python -m jobpipe.cli verify-sources

ingest:      ## pull from all sources -> normalize -> dedup -> DB
	python -m jobpipe.cli ingest

score:       ## keyword prefilter, then LLM score on survivors
	python -m jobpipe.cli score

prepare:     ## tailor resume + cover note for shortlisted jobs
	python -m jobpipe.cli prepare

notify:      ## push today's review queue to Telegram
	python -m jobpipe.cli notify

track:       ## parse Gmail for replies, update statuses, flag follow-ups
	python -m jobpipe.cli track

review:      ## local review dashboard on :8080
	uvicorn jobpipe.review_api:app --host 0.0.0.0 --port 8080

all: ingest score prepare notify
