"""Which AI models were actually called, by JobPipe and its sibling ContentPipe.

The budget file (llm.STATE_FILE) keeps only today's per-model COUNT and resets at
midnight Pacific, so "what ran in the last 48 hours" was unanswerable. Every
request a provider receives is now appended here, one JSON line each:

    {"t": "2026-09-27T14:40:02Z", "app": "jobpipe", "provider": "gemini",
     "model": "gemini-flash-latest", "outcome": "ok", "ms": 2310}

`outcome` uses ContentPipe's vocabulary (ok | quota | overloaded | error |
invalid_output) so the two logs aggregate as one. A row may carry `count` (one
line standing for several calls) and `source: "reconstructed"` -- the one-off
backfill of the 48 hours before this log existed, rebuilt from daily logs and
the video-burn log, which only know totals.

ContentPipe writes the same shape to <ContentPipe>/.runs/model-usage.jsonl
(server/llm/usage.ts); `aggregate()` reads both for the review dashboard.
Writing must never break a model call: every failure to log is swallowed.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DATA_DIR, ROOT, env

OUTCOMES = ("ok", "quota", "overloaded", "error", "invalid_output")
# Past this size a log moves to "<file>.1", replacing the previous one (ContentPipe does the same,
# USAGE_LOG_MAX_BYTES in server/llm/usage.ts); aggregate() reads both, so a rotation never blanks the panel.
MAX_BYTES = 5 * 1024 * 1024


def log_path() -> Path:
    return Path(env("JOBPIPE_USAGE_LOG", str(DATA_DIR / "llm_usage.jsonl")))


def contentpipe_log_path() -> Path:
    base = Path(env("CONTENTPIPE_DIR", str(ROOT.parent / "ContentPipe")))
    return base / ".runs" / "model-usage.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record(provider: str, model: str, outcome: str, *, ms: int | None = None,
           detail: str | None = None, task: str | None = None) -> None:
    """Append one call. A request that was never sent (a spent budget) is not a call."""
    row = {"t": _now().strftime("%Y-%m-%dT%H:%M:%SZ"), "app": "jobpipe",
           "provider": provider, "model": model, "outcome": outcome}
    if ms is not None:
        row["ms"] = int(ms)
    if detail:
        row["detail"] = " ".join(str(detail).split())[:200]
    if task:
        row["task"] = task
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.stat().st_size >= MAX_BYTES:
                os.replace(p, p.with_name(p.name + ".1"))
        except FileNotFoundError:
            pass
        # One short write with O_APPEND: concurrent processes (score by cron, prepare by
        # hand) interleave whole lines, never halves of them.
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _read(path: Path, since: datetime) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    t = datetime.strptime(r["t"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                except (ValueError, KeyError, TypeError):
                    continue          # a torn or foreign line is skipped, never fatal
                if t >= since:
                    r["_t"] = t
                    rows.append(r)
    except OSError:
        pass
    return rows


def aggregate(hours: int = 48, now: datetime | None = None) -> dict:
    """One row per (app, provider, model) called in the window, busiest first within each app."""
    now = now or _now()
    since = now - timedelta(hours=hours)
    rows = []
    for p in (log_path(), contentpipe_log_path()):
        rows += _read(p.with_name(p.name + ".1"), since) + _read(p, since)
    groups: dict[tuple, dict] = defaultdict(lambda: {"calls": 0, "ok": 0, "failed": 0, "last_used": None,
                                                       "last_problem": None, "reconstructed": 0, "tasks": set()})
    for r in rows:
        key = (r.get("app", "?"), r.get("provider", "?"), r.get("model", "?"))
        g = groups[key]
        n = int(r.get("count") or 1)
        g["calls"] += n
        if r.get("outcome") == "ok":
            g["ok"] += n
        else:
            g["failed"] += n
            if not g["last_problem"] or r["_t"] >= g["last_problem"][0]:
                g["last_problem"] = (r["_t"], f"{r.get('outcome')}: {r.get('detail') or ''}".rstrip(": "))
        if r.get("source") == "reconstructed":
            g["reconstructed"] += n
        if r.get("task"):
            g["tasks"].add(r["task"])
        if not g["last_used"] or r["_t"] > g["last_used"]:
            g["last_used"] = r["_t"]
    models = []
    for (app, provider, model), g in groups.items():
        models.append({
            "app": app, "provider": provider, "model": model,
            "calls": g["calls"], "ok": g["ok"], "failed": g["failed"],
            "last_used": g["last_used"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_problem": g["last_problem"][1] if g["last_problem"] else None,
            "reconstructed": g["reconstructed"],
            "tasks": sorted(g["tasks"])[:6],
        })
    models.sort(key=lambda m: (m["app"], -m["calls"], m["model"]))
    return {"hours": hours, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "models": models,
            "sources": {"jobpipe": str(log_path()), "contentpipe": str(contentpipe_log_path())}}
