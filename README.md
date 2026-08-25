<div align="center">

# jobpipe

**A job-search pipeline that automates everything except the submit button.**

Discovery, deduplication, fit scoring, resume tailoring, screening answers and
reply tracking all run unattended.
You apply, on the employer's own site, from a queue of fifteen.

[![tests](https://github.com/Asit0007/JobPipe/actions/workflows/tests.yml/badge.svg)](https://github.com/Asit0007/JobPipe/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![cost](https://img.shields.io/badge/running%20cost-%240-success)](#cost)

</div>

---

## The one rule

> **Nothing in this codebase submits an application, logs into a job board,
> drives a browser session, or stores a job-board password.**

That is a deliberate design constraint, not an unfinished feature.

Tools that drive your logged-in LinkedIn or Naukri session are the ones that get
accounts restricted — verification email, then outreach disabled for weeks, then
sometimes a lockout that never gets reversed. The asymmetry is the whole
argument: losing your LinkedIn account mid-search is the worst outcome
available, and auto-submit buys throughput on Easy Apply, the lowest-conversion
channel there is.

jobpipe works one layer down, on the **postings**. Public ATS APIs, an
aggregator, and job-alert emails **you** configured, read out of **your own**
inbox. There is no activity on any job board, so there is nothing to detect and
nothing to ban.

`review_api.py` is the only module that can write `status = applied`, and only
in response to you clicking a button after you have already submitted.

<sub>CI enforces this: a workflow fails the build if `selenium`, `playwright`,
`puppeteer` or a board credential ever appears in the source tree.</sub>

---

## How it works

```mermaid
flowchart TD
    A["<b>sources</b><br/>greenhouse · lever · ashby<br/>adzuna · gmail alerts"] --> B["<b>normalize</b><br/>canonicalise · fingerprint · dedup"]
    B --> C[("<b>discovered</b>")]
    C --> D{"<b>prefilter</b><br/>free, deterministic<br/>no LLM call spent"}
    D -->|"81% die here"| E["filtered"]
    D -->|"19% survive"| F["<b>score</b><br/>LLM fit rating 0-100"]
    F --> G[("<b>shortlisted</b>")]
    G --> H["<b>tailor</b><br/>fact-gated selection<br/>+ screening answers"]
    H --> I[("<b>prepared</b>")]
    I --> J["<b>notify</b> → Telegram"]
    J --> K((("<b>YOU APPLY</b><br/>on the real posting")))
    K --> L[("<b>applied</b>")]
    L --> M["<b>track</b><br/>Gmail reply classification"]
    M --> N[("<b>responded</b>")]

    style K fill:#E8A33D,stroke:#8a5d00,stroke-width:3px,color:#1a1a1a
    style D fill:#1B2330,stroke:#4FB3C9,color:#D9E1EA
    style E fill:#2a2020,stroke:#5A6675,color:#78899B
```

### The funnel is the product

Measured on a real run against 45 live ATS boards:

| stage | count | |
|---|---:|---|
| ingested | **4,654** | 45 boards, deduplicated |
| killed on title | −1,713 | sales roles whose JD lists your whole toolchain |
| killed on keywords | −1,681 | fewer than 2 must-haves present |
| killed on hard rejects | −392 | seniority, shift work, geography |
| **reach an LLM call** | **868** | 19% — *this is what protects the free tier* |
| shortlisted | ~40–60 | above `shortlist_min_score` |
| **queued for you** | **15/day** | a hard cap, because volume is not the goal |

Every row killed above the LLM line costs nothing. That's the point: the free
tier gives roughly 15 requests per minute, so the prefilter is what makes a full
run take one hour instead of six.

---

## The facts gate

An LLM asked to "tailor my resume to this JD" will quietly invent metrics, team
sizes and durations. Those become interview questions you cannot answer.

jobpipe makes that **structurally impossible** rather than merely discouraged:

```yaml
# config/facts.yaml — the only source of resume claims
- {id: F005, verified: false, tags: [ansible, automation],
   text: "Wrote Ansible playbooks to replace manual configuration steps"}
```

The tailoring model never writes experience. It receives a **menu of fact IDs**
and must return a *selection*. `tailor.py` validates every ID that comes back
and drops anything it doesn't recognise.

Facts ship `verified: false` and are excluded from the menu entirely until you
flip them. The bar is one question:

> *Could you defend this line if an interviewer stopped you on it?*

That friction is the feature. `facts.yaml` is also schema-validated on load — a
typo'd ID or a missing `verified` key fails loudly instead of silently dropping
the fact.

### Questions it refuses to answer for you

Notice period, current CTC, expected CTC, relocation, reason for leaving.

These are negotiating positions, not form fields. They surface as a reminder on
every prepared application, and you fill them in yourself. See `HUMAN_ONLY` in
`screening.py`.

---

## Setup

Everything is free tier. No card, anywhere.

### 1. Install

```bash
git clone https://github.com/Asit0007/JobPipe.git jobpipe && cd jobpipe
python3 -m venv .venv && source .venv/bin/activate
make install
make config          # creates .env + your private config from the templates
```

`config/profile.yaml` and `config/facts.yaml` are gitignored — they hold your
work history and your salary target.

### 2. Gemini key, then let the doctor tell you the rest

Paste your key into `.env` as `GEMINI_API_KEY`, then:

```bash
make doctor
```

It reports your tier, lists the models your key can *actually* call, and prints
the exact `MODEL_SCORE` / `MODEL_TAILOR` / `GEMINI_RPM` block to paste back into
`.env`. Re-run it until every check is green.

> On the free tier Google may train on your prompts. `llm.py` strips emails,
> phone numbers, ID numbers and profile URLs from every prompt before it leaves
> the process. Add your employer's name to `PII_DENY_TERMS` in `.env`.

### 3. Fill in the two files that matter

**`config/profile.yaml`** — titles, locations, thresholds, and the three reject
lists. Tune `title_reject` and `hard_reject` aggressively; every term you add
saves quota for free.

**`config/facts.yaml`** — see [the facts gate](#the-facts-gate). Nothing can be
prepared until at least one fact is verified. `make doctor` tells you the count.

### 4. Wire the sources

```bash
make verify          # pings every ATS slug, prints the dead ones
```

Prune whatever comes back `DEAD`, then add companies you actually want. Open a
careers page and read the URL:

| URL | put the slug under |
|---|---|
| `boards.greenhouse.io/SLUG` | `greenhouse:` |
| `jobs.lever.co/SLUG` | `lever:` |
| `jobs.ashbyhq.com/SLUG` | `ashby:` |

<details>
<summary><b>Adzuna</b> — optional, 1000 free calls/month, covers the India market</summary>

Register at [developer.adzuna.com](https://developer.adzuna.com), put the app id
and key in `.env`. Adzuna returns snippets rather than full JDs, so its scores
are weighted down automatically.
</details>

<details>
<summary><b>Gmail</b> — optional, and the only way LinkedIn and Naukri get in</summary>

1. [console.cloud.google.com](https://console.cloud.google.com) → new project → enable the Gmail API
2. Credentials → OAuth client ID → **Desktop app** → download the JSON
3. Save it as `data/gmail_credentials.json`
4. Set up saved-search alerts on LinkedIn and Naukri, set to daily email
5. First run opens a browser once for consent

The scope is **read-only**. You configure the searches on their site; they email
you; jobpipe reads your own inbox. Zero account activity on either platform.
</details>

<details>
<summary><b>Telegram</b> — optional, for the daily queue</summary>

[@BotFather](https://t.me/botfather) → `/newbot` → token into `.env`.
[@userinfobot](https://t.me/userinfobot) for your chat id. Without it, the queue
prints to stdout.
</details>

### 5. Run it

```bash
make ingest      # pull every source, normalize, dedup
make score       # free prefilter, then LLM on the survivors
make prepare     # tailored bullets + screening answers
make notify      # push the day's shortlist to Telegram
make review      # dashboard on 127.0.0.1:8080
```

Or `make all`. `make status` shows pipeline counts and remaining quota.

---

## The daily loop

Telegram pings you at 07:00 with up to fifteen roles. Each shows the fit score,
the deciding reason, your genuine skill gaps, and a link.

Open `make review`, expand the prepared bullets and screening answers, apply on
the company's site, click **Mark applied**.

**Budget 45 minutes.** Fifteen tailored applications beat two hundred blind ones
— and the applying is the part no pipeline can do for you.

> **One thing this tool cannot do.** Referrals convert roughly 10–20× better
> than cold applications, and jobpipe is deliberately optimising the
> second-best channel. Don't let a wall of green checkmarks substitute for
> messaging someone who works there.

---

## Deployment

**Recommended — an always-free Oracle Cloud ARM box:**

```bash
rsync -av --exclude .venv --exclude data --exclude .git ./ ubuntu@<ip>:~/jobpipe/
ssh ubuntu@<ip> 'cd ~/jobpipe && bash deploy/oci-setup.sh'
```

The dashboard binds to `127.0.0.1` only. Reach it through a Cloudflare Tunnel
behind Cloudflare Access — no open ports, no ingress rule. `deploy/crontab`
drives the schedule via supercronic, so runs happen at 07:00 IST and stay there.

**Fallback — GitHub Actions.** `.github/workflows/pipeline.yml` runs weekdays at
07:00 IST. It works, with one real caveat: runners are ephemeral, so the SQLite
file that *is* the dedup layer rides in the Actions cache. On a cache miss every
posting looks new. Fine as a backup, wrong as a home.

**Not Vercel.** Rate-limited scoring of 100 jobs takes 8–10 minutes against a
~60s function ceiling, and the SQLite file would reset on every invocation.
See [`deploy/VERCEL.md`](deploy/VERCEL.md).

---

## Cost

| | |
|---|---|
| Gemini | free tier — ~15 RPM, 1500 RPD, budget-capped to 1200 |
| ATS APIs | public, no key, no quota |
| Adzuna | free tier, 1000 calls/month |
| Gmail API | free, read-only scope |
| Telegram | free |
| Hosting | OCI always-free ARM |
| **total** | **$0** |

A daily budget counter persists to disk behind a file lock, so a runaway retry
loop can't burn the quota at 3am and leave you with nothing at 9am. The rate
limiter and the counter are shared across processes — the scheduler and a manual
run can overlap without doubling your real request rate.

---

## What it deliberately will not do

- Submit an application anywhere
- Store a LinkedIn, Naukri or Indeed password
- Drive a logged-in browser session on any job board
- Answer notice period, CTC or relocation questions on your behalf
- Use an unverified fact, or a fact ID the model invented

---

## Layout

```
config/profile.yaml       search definition, three reject lists, thresholds
config/facts.yaml         the verified-fact menu — the anti-hallucination boundary
config/companies.yaml     ATS slugs (the part you maintain by hand)
scripts/doctor.py         preflight: key, tier, models, config, integrations

src/jobpipe/
  llm.py                  Gemini over raw REST — rate limit, budget, PII redaction
  sources/                greenhouse · lever · ashby · adzuna · gmail alerts
  normalize.py            canonicalisation, fingerprinting, fuzzy dedup
  jdfetch.py              descriptions for postings that arrive without one
  score.py                free prefilter → LLM scoring
  tailor.py               fact-bounded tailoring + the validation gate
  screening.py            screening answers; human-only questions carved out
  notify.py               Telegram review queue
  track.py                reply classification + follow-up nudges
  review_api.py           dashboard; the sole writer of status=applied

deploy/                   OCI setup, crontab, cloudflared example
```

**Stack** — Python 3.12 · SQLite · httpx · FastAPI · Gemini (raw REST) ·
rapidfuzz · Docker Compose · Cloudflare Tunnel

```bash
make test          # 42 tests
```

---

<div align="center">
<sub>Built for one job search. The submit button stays human.</sub>
</div>
