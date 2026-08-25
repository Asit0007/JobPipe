# jobpipe

A job-search pipeline that automates everything except the submit button.

Discovery, deduplication, fit scoring, resume tailoring, screening answers and
reply tracking all run unattended. Applications are submitted by you, on the
employer's own site. Running cost: zero.

---

## Why the submit button stays manual

Tools that drive your logged-in LinkedIn or Naukri session are the ones that get
accounts restricted. This pipeline never touches those sessions and never stores
a board password — LinkedIn and Naukri enter through *job-alert emails you
configure yourself*, read out of your own Gmail. There is nothing for either
platform to detect, because there is no activity on either platform.

`review_api.py` is the only module that can write `status = applied`, and it does
so only in response to you clicking a button after you have already submitted.

---

## Setup

Everything below is free tier. No card, anywhere.

### 1. Clone and install

```bash
git clone <your-private-repo> jobpipe && cd jobpipe
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Keep the repo **private**. `config/facts.yaml` holds your work history.

### 2. Gemini key

Paste your AI Studio key into `.env` as `GEMINI_API_KEY`.

Free tier is Flash and Flash-Lite only (Pro left the free tier in April 2026),
at roughly 15 RPM and 1500 requests/day. `GEMINI_RPM=12` and
`GEMINI_RPD_BUDGET=1200` keep you under both ceilings with margin. The daily
counter persists to `data/gemini_budget.json`, so a runaway loop can't burn the
quota overnight.

**Free-tier prompts may be used to train Google's models.** `llm.py` strips
emails, phone numbers, ID numbers and profile URLs from every prompt before it
leaves the process. Add anything else you want scrubbed to `PII_DENY_TERMS` in
`.env` as a comma-separated list — your employer name is a reasonable one.

### 3. Fill in the two config files that matter

**`config/profile.yaml`** — titles, locations, salary target, hard rejects.
Tune `hard_reject` aggressively; every term you add saves LLM quota for free.

**`config/facts.yaml`** — this is the important one. Every fact ships with
`verified: false` and is **blocked from use** until you flip it to `true`.
Read each line and ask: *can I defend this in a live interview?* That gate is
what stops a tailored resume from drifting into a fabricated one. `tailor.py`
validates every fact ID the model returns and drops anything it invented.

### 4. Wire the sources

```bash
make verify     # pings every ATS slug, prints the dead ones
```

Fix or delete whatever comes back DEAD, then add companies you actually want.
Open a careers page and read the URL: `boards.greenhouse.io/SLUG` → greenhouse,
`jobs.lever.co/SLUG` → lever, `jobs.ashbyhq.com/SLUG` → ashby.

**Adzuna** (optional, 1000 free calls/month): register at
developer.adzuna.com, put the app id and key in `.env`.

**Gmail** (this is how LinkedIn and Naukri get in):
1. console.cloud.google.com → new project → enable the Gmail API
2. Credentials → OAuth client ID → Desktop app → download JSON
3. Save it as `data/gmail_credentials.json`
4. Set up saved-search job alerts on LinkedIn and Naukri, set to daily email
5. First run opens a browser once for consent. Scope is read-only.

### 5. Telegram (optional)

Message @BotFather → `/newbot` → token into `.env`. Message @userinfobot for
your chat id. Without it, the queue prints to stdout instead.

### 6. Run it

```bash
make ingest    # pull every source, normalize, dedup
make score     # free prefilter, then Gemini on survivors
make prepare   # tailored bullets + screening answers
make notify    # push the day's shortlist to Telegram
make review    # dashboard on localhost:8080
```

Or `make all`. `python -m jobpipe.cli status` shows pipeline state and
remaining quota.

---

## Free hosting

**Option A — your OCI always-free VM** (you already run one):
```bash
docker compose up -d
```
Expose the dashboard through your existing Cloudflare Tunnel. The `scheduler`
service runs the full pipeline daily.

**Option B — GitHub Actions.** `.github/workflows/pipeline.yml` runs weekdays at
07:00 IST. Private repos get 2000 free minutes/month; this uses about 200.
Add your keys under Settings → Secrets → Actions.

Option A is better here — the DB persists properly and no secrets leave your VM.

---

## Daily loop

Telegram pings you at 07:00 with up to 15 roles. Each one shows the fit score,
the deciding reason, your skill gaps, and a link. Open `make review`, expand the
prepared bullets, apply on the company site, click **Mark applied**.

Budget 45 minutes. Fifteen tailored applications beat two hundred blind ones,
and this is the part no pipeline can do for you.

---

## What it deliberately will not do

- Submit an application anywhere
- Store a LinkedIn, Naukri or Indeed password
- Drive a logged-in browser session on any job board
- Answer notice period, current CTC, expected CTC, or relocation questions —
  those are negotiating positions, not form fields. See `HUMAN_ONLY` in
  `screening.py`.
- Use an unverified fact, or a fact ID the model made up

---

## Layout

```
config/profile.yaml     search definition, hard rejects, thresholds
config/facts.yaml       verified-fact menu — the anti-hallucination boundary
config/companies.yaml   ATS slugs (the part you maintain by hand)

src/jobpipe/
  llm.py                Gemini: rate limit, daily budget, PII redaction
  sources/              greenhouse, lever, ashby, adzuna, gmail alerts
  normalize.py          canonicalisation + dedup
  score.py              free prefilter -> Gemini scoring
  tailor.py             fact-bounded resume tailoring
  screening.py          screening answers; human-only questions carved out
  notify.py             Telegram review queue
  track.py              reply classification + follow-up nudges
  review_api.py         the only writer of status=applied
```
