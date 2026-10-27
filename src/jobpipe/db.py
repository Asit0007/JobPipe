"""SQLite access layer. Single-user tool, so no ORM and no server."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DB_PATH

# Fields a later, richer sighting of the same posting is allowed to fill in.
# An email alert gives us a title and a link; the same job arriving from
# Greenhouse two hours later carries the full JD. Before this, the fingerprint
# matched, only last_seen moved, and the good description was thrown away --
# which is why the whole LinkedIn/Naukri path produced nothing usable.
BACKFILLABLE = ("description", "salary_raw", "apply_url", "posted_at", "location")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(timespec="seconds")


def init() -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text()
    with connect() as c:
        c.executescript(schema)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_job(job: dict) -> str:
    """Insert new, or bump last_seen (and backfill empties) on a known fingerprint.

    Returns 'inserted', 'enriched' or 'seen'. Never overwrites a non-empty
    field, and never touches a human-set field.
    """
    with connect() as c:
        row = c.execute(
            "SELECT * FROM jobs WHERE fingerprint = ?", (job["fingerprint"],)
        ).fetchone()

        if row:
            fills = {}
            for col in BACKFILLABLE:
                incoming = (job.get(col) or "").strip() if isinstance(job.get(col), str) else job.get(col)
                existing = (row[col] or "").strip() if isinstance(row[col], str) else row[col]
                if incoming and not existing:
                    fills[col] = incoming
            sets = ", ".join(f"{k} = ?" for k in fills)
            c.execute(
                f"UPDATE jobs SET last_seen = ?{', ' + sets if fills else ''} WHERE id = ?",
                (now(), *fills.values(), row["id"]),
            )
            return "enriched" if fills else "seen"

        cols = [
            "fingerprint", "source", "source_id", "company", "company_canonical",
            "title", "location", "remote", "url", "apply_url", "description",
            "salary_raw", "posted_at",
        ]
        vals = [job.get(k) for k in cols]
        placeholders = ",".join("?" * (len(cols) + 2))
        c.execute(
            f"INSERT INTO jobs ({','.join(cols)}, first_seen, last_seen) "
            f"VALUES ({placeholders})",
            (*vals, now(), now()),
        )
        return "inserted"


def fetch(status: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
    q = "SELECT * FROM jobs"
    args: tuple = ()
    if status:
        q += " WHERE status = ?"
        args = (status,)
    # `NULLS LAST` needs SQLite >= 3.30. This form works everywhere.
    q += " ORDER BY score IS NULL, score DESC, first_seen DESC LIMIT ?"
    with connect() as c:
        return c.execute(q, (*args, limit)).fetchall()


def recent_by_company(company_canonical: str, within_days: int = 30) -> list[sqlite3.Row]:
    """Candidate rows for the fuzzy near-duplicate check in ingest."""
    with connect() as c:
        return c.execute(
            "SELECT id, company, title, location FROM jobs "
            "WHERE company_canonical = ? AND first_seen >= ?",
            (company_canonical, days_ago(within_days)),
        ).fetchall()


# Statuses after which a vacancy is settled by the human: applied to, heard
# back from, or passed on. A twin of one of these is not listed again.
CLOSED = ("applied", "responded", "skipped")


def listing_keys(statuses: tuple[str, ...]) -> set[tuple[str, str]]:
    """normalize.listing_key of every row in `statuses`. Recomputed from company
    and title, not the stored company_canonical (a cached value, see §10)."""
    from .normalize import listing_key
    marks = ",".join("?" * len(statuses))
    with connect() as c:
        return {listing_key(r["company"], r["title"]) for r in c.execute(
            f"SELECT company, title FROM jobs WHERE status IN ({marks})", statuses)}


def archive_stale(days: int, statuses: tuple[str, ...] = ("discovered", "scored", "shortlisted")) -> int:
    """Retire postings nobody is going to apply to any more.

    Uses last_seen, not posted_at: many sources give no posted_at at all, and a
    posting that stopped appearing in the feed is the better staleness signal.
    Never touches prepared/queued/applied -- those are the human's to close.
    """
    marks = ",".join("?" * len(statuses))
    with connect() as c:
        cur = c.execute(
            f"UPDATE jobs SET status = 'stale' "
            f"WHERE status IN ({marks}) AND last_seen < ?",
            (*statuses, days_ago(days)),
        )
        return cur.rowcount


def update(job_id: int, **fields) -> None:
    if not fields:
        return
    sets = ",".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))


def seen_replies(message_ids: list[str]) -> set[str]:
    """Which of these messages track.py has already decided about."""
    if not message_ids:
        return set()
    with connect() as c:
        found = set()
        for i in range(0, len(message_ids), 500):   # SQLite's variable limit
            chunk = message_ids[i:i + 500]
            found |= {r[0] for r in c.execute(
                f"SELECT message_id FROM replies WHERE message_id IN ({','.join('?' * len(chunk))})",
                chunk)}
        return found


def add_reply(message_id: str, sender: str, subject: str, kind: str,
              received_at: str | None = None, job_id: int | None = None) -> None:
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO replies (message_id, received_at, sender, subject, "
            "kind, job_id, seen_at) VALUES (?,?,?,?,?,?,?)",
            (message_id, received_at, sender, subject, kind, job_id, now()))


def unsent_replies() -> list[sqlite3.Row]:
    """Everything worth telling the user about that Telegram has not taken yet,
    oldest first. Includes rows a failed send left behind on an earlier run."""
    with connect() as c:
        return c.execute(
            "SELECT r.*, j.company AS job_company, j.title AS job_title "
            "FROM replies r LEFT JOIN jobs j ON j.id = r.job_id "
            "WHERE r.kind != 'noise' AND r.sent_at IS NULL "
            "ORDER BY r.received_at IS NULL, r.received_at").fetchall()


def mark_replies_sent(message_ids: list[str]) -> None:
    with connect() as c:
        c.executemany("UPDATE replies SET sent_at = ? WHERE message_id = ?",
                      [(now(), m) for m in message_ids])


def log_run(stage: str, ok: bool, detail: str = "") -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO runs (stage, started_at, ended_at, ok, detail) VALUES (?,?,?,?,?)",
            (stage, now(), now(), int(ok), detail),
        )
