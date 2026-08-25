# Why this doesn't run on Vercel

Short version: the pipeline is a long-running stateful batch job, and Vercel
functions are short-lived and stateless. Four hard blockers:

**1. Execution time.** Free-tier Gemini caps at ~15 RPM, so `llm.py` paces calls
sequentially through a token bucket. Scoring 100 postings takes roughly 8-10
minutes. Vercel Hobby functions cap out around 60 seconds. The job dies mid-run,
every run.

**2. No persistent filesystem.** The SQLite DB is how deduplication works — it
remembers which postings it has already seen. Vercel's filesystem is ephemeral
and resets between invocations, so every run would treat every posting as new,
re-score it, and burn your quota re-doing yesterday's work.

**3. The Gmail OAuth token.** `data/gmail_token.json` has to survive between
runs. On Vercel it doesn't.

**4. Cron granularity.** Hobby cron is once-daily, which is workable, but the
timeout in (1) makes it moot.

## If you specifically want Vercel

Only the dashboard can go there, and only after the DB moves off local disk:

- Swap SQLite for **Turso** (libSQL, generous free tier) or **Neon** Postgres
  (free tier). Rewrite `db.py` to use the remote driver.
- Keep `ingest / score / prepare / notify` on OCI or GitHub Actions writing to
  that remote DB.
- Deploy only `review_api.py` to Vercel, reading the same DB.

That's a real architecture, but it's a second project. Everything already works
on OCI today, on hardware you're already paying nothing for.

## GitHub Actions as the middle ground

`.github/workflows/pipeline.yml` is included and works. Caveats:
- 6-hour job limit, so the timeout problem disappears
- `actions/cache` for the DB is best-effort — caches get evicted after 7 days of
  no access, and dedup history goes with it
- Secrets live on GitHub rather than your own box

Fine as a fallback. OCI is better.
