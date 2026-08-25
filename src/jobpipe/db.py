"""SQLite access layer. Single-user tool, so no ORM and no server."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    """Insert new, or bump last_seen on an existing fingerprint.

    Returns 'inserted' or 'seen'. Never overwrites human-set fields.
    """
    with connect() as c:
        row = c.execute(
            "SELECT id FROM jobs WHERE fingerprint = ?", (job["fingerprint"],)
        ).fetchone()
        if row:
            c.execute("UPDATE jobs SET last_seen = ? WHERE id = ?", (now(), row["id"]))
            return "seen"

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
    q += " ORDER BY score DESC NULLS LAST, first_seen DESC LIMIT ?"
    with connect() as c:
        return c.execute(q, (*args, limit)).fetchall()


def update(job_id: int, **fields) -> None:
    if not fields:
        return
    sets = ",".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))


def log_run(stage: str, ok: bool, detail: str = "") -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO runs (stage, started_at, ended_at, ok, detail) VALUES (?,?,?,?,?)",
            (stage, now(), now(), int(ok), detail),
        )
