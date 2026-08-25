-- Job lifecycle:
--   discovered -> scored -> shortlisted -> prepared -> queued
--   -> applied (SET BY YOU, never by the pipeline) -> responded
--
-- Terminal side-branches:
--   filtered  killed by the free prefilter, no LLM call spent
--   stale     not seen in `stale_after_days`; archived, never shown again
--   skipped   you looked at it in the dashboard and passed
--
-- Nothing in this codebase writes 'applied'. That transition happens only when
-- you click the button in the review UI, after you have submitted it yourself.

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint       TEXT UNIQUE NOT NULL,   -- dedup key
    source            TEXT NOT NULL,          -- greenhouse | lever | adzuna | gmail_alert | ...
    source_id         TEXT,
    company           TEXT NOT NULL,
    company_canonical TEXT NOT NULL,
    title             TEXT NOT NULL,
    location          TEXT,
    remote            INTEGER DEFAULT 0,
    url               TEXT NOT NULL,
    apply_url         TEXT,
    description       TEXT,
    salary_raw        TEXT,
    posted_at         TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,

    status            TEXT NOT NULL DEFAULT 'discovered',
    prefilter_hits    INTEGER,
    score             INTEGER,
    score_reason      TEXT,
    missing_skills    TEXT,
    red_flags         TEXT,
    tailor_notes      TEXT,

    resume_path       TEXT,
    cover_path        TEXT,
    fact_ids_used     TEXT,

    notified_at       TEXT,
    applied_at        TEXT,          -- written only by the human 'Mark applied' action
    response_at       TEXT,
    response_kind     TEXT,          -- rejection | recruiter | interview | offer
    followup_due      TEXT,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_canonical);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stage      TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    ok         INTEGER,
    detail     TEXT
);
