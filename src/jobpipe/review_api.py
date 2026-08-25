"""Local review dashboard. The only place an application is ever marked applied.

Deliberately has no submit capability of any kind. It opens the real posting in
a new tab; you apply there, come back, and record it.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import db
from .config import TEMPLATE_DIR
from .db import now

app = FastAPI(title="jobpipe review")


@app.on_event("startup")
def _startup():
    db.init()


@app.get("/", response_class=HTMLResponse)
def index():
    return (TEMPLATE_DIR / "dashboard.html").read_text()


@app.get("/api/queue")
def queue():
    out = []
    for status in ("queued", "prepared"):
        for r in db.fetch(status=status, limit=50):
            prep = ""
            if r["resume_path"] and Path(r["resume_path"]).exists():
                prep = Path(r["resume_path"]).read_text()
            out.append({
                "id": r["id"], "title": r["title"], "company": r["company"],
                "location": r["location"], "score": r["score"],
                "reason": r["score_reason"], "url": r["apply_url"] or r["url"],
                "missing": json.loads(r["missing_skills"] or "[]"),
                "flags": json.loads(r["red_flags"] or "[]"),
                "source": r["source"], "prep": prep,
            })
    out.sort(key=lambda x: x["score"] or 0, reverse=True)
    return JSONResponse(out)


@app.post("/api/jobs/{job_id}/applied")
def mark_applied(job_id: int):
    """Human action only. No code path anywhere else writes this status."""
    db.update(job_id, status="applied", applied_at=now())
    return {"ok": True}


@app.post("/api/jobs/{job_id}/skip")
def skip(job_id: int):
    db.update(job_id, status="skipped")
    return {"ok": True}


@app.get("/api/stats")
def stats():
    from .llm import budget_remaining
    with db.connect() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
    return {"by_status": {r["status"]: r["n"] for r in rows},
            "gemini_left": budget_remaining()}
