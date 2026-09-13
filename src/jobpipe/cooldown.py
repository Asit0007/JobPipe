"""One application per company, then a wait before the next.

Asit's rule, 2026-09-13: applying to a company starts a 90-day clock, and
another role there is off the table until it expires. The motivating evidence
was 35 of 372 shortlisted rows sitting at 15 companies already applied to --
Cloudflare 6, Infosys 4, CGI 3, all applied within two weeks -- plus a real
duplicate application to ProArch on 2026-09-08 that the location-level dedup
deliberately cannot catch (one row said "India", the other "Hyderabad,
Telangana", and those are correctly two places).

**This layer NEVER blocks anything, and that is the decision, not an
oversight.** Every role still scores, prepares and queues; the cooldown is
reported beside it and the human decides. §2's whole design is that nothing
here overrides the person -- and a filter that silently removes a role is
indistinguishable from a pipeline that found nothing, which is the failure mode
this project has hit six times.

It is written as a pure lookup precisely so that turning it INTO a filter later
is a call site change, not a rewrite.

Two signals, deliberately distinct, because you act on them differently:

    cooldown   you APPLIED here on <date>; next allowed <date + N days>
    in_queue   you have an unsent document for this company RIGHT NOW

The second is a duplicate-effort warning, not a cooldown. Collapsing them into
one flag would hide the case where you are about to prepare a second document
for a company whose first one you have not sent yet.
"""
from __future__ import annotations

import datetime as _dt
from functools import lru_cache

from . import db
from .config import CONFIG_DIR, _load, profile
from .normalize import canon_company

DEFAULT_COOLDOWN_DAYS = 90

# Statuses that mean "a document exists for this company but you have not
# applied yet". `shortlisted` is NOT one of them -- nothing has been spent yet.
IN_FLIGHT = ("prepared", "queued")


def cooldown_days() -> int:
    return int(profile().get("thresholds", {})
               .get("company_cooldown_days", DEFAULT_COOLDOWN_DAYS))


def _date(value: str | None) -> _dt.date | None:
    """Tolerant date parse. applied_at is an ISO timestamp; the manual file is
    a plain date. A row with an unreadable date must not crash the queue."""
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


@lru_cache(maxsize=None)
def manual_entries() -> tuple[dict, ...]:
    """Applications made OUTSIDE JobPipe -- referrals, direct email, recruiters.

    The pipeline cannot know about these, and they are exactly where a
    forgotten application hides: you are far more likely to lose track of a
    referral you sent in a DM than of a row you clicked Mark applied on.

    Missing file is not an error. Someone who never applies outside the
    pipeline should not have to create it.
    """
    if not (CONFIG_DIR / "applied_companies.yaml").exists():
        return ()
    raw = _load("applied_companies.yaml") or {}
    out = []
    for e in raw.get("applied", []) or []:
        if not isinstance(e, dict) or not e.get("company"):
            continue
        out.append({
            "company": str(e["company"]),
            "canonical": canon_company(str(e["company"])),
            "applied": _date(e.get("applied")),
            "role": e.get("role") or "",
            "note": e.get("note") or "",
            "manual": True,
        })
    return tuple(out)


def applied_index() -> dict[str, list]:
    """canonical company -> its applications, most recent first.

    A LIST, not just the latest one, for a reason that only shows up on the
    document for an already-applied job: that row is itself the most recent
    application to its company, so a "latest only" index made every such
    document announce that you had applied to this company -- naming the very
    role the document was for. `check()` skips the row it is asked about and
    falls through to the runner-up, which is the genuinely useful case: you
    applied to Cloudflare in March, and this is the second Cloudflare role.

    Most recent first, because the clock runs from the last time you
    approached them -- a second application restarts the wait rather than
    inheriting the original expiry.
    """
    index: dict[str, list] = {}

    def offer(canonical, entry):
        if canonical:
            index.setdefault(canonical, []).append(entry)

    with db.connect() as c:
        for r in c.execute(
            "SELECT id, company, company_canonical, title, applied_at FROM jobs "
            "WHERE status = 'applied'"
        ):
            offer(r["company_canonical"] or canon_company(r["company"]), {
                "id": r["id"], "company": r["company"],
                "applied": _date(r["applied_at"]),
                "role": r["title"], "note": "", "manual": False,
            })

    for e in manual_entries():
        offer(e["canonical"], {**e, "id": None})

    for entries in index.values():
        entries.sort(key=lambda e: e["applied"] or _dt.date.min, reverse=True)
    return index


def in_flight_index() -> dict[str, dict]:
    """canonical company -> a document prepared or queued but not yet applied."""
    index: dict[str, dict] = {}
    with db.connect() as c:
        for r in c.execute(
            "SELECT id, company, company_canonical, title, status FROM jobs "
            f"WHERE status IN ({','.join('?' * len(IN_FLIGHT))})", IN_FLIGHT
        ):
            key = r["company_canonical"] or canon_company(r["company"])
            index.setdefault(key, {"company": r["company"], "role": r["title"],
                                   "status": r["status"], "id": r["id"]})
    return index


def check(company: str, *, job_id: int | None = None,
          applied=None, in_flight=None, today: _dt.date | None = None) -> dict | None:
    """The flag for one company, or None if there is nothing to say.

    Pass `applied`/`in_flight` when checking many rows -- each call would
    otherwise re-read the table, which is how a queue render turns into N
    queries.
    """
    key = canon_company(company or "")
    if not key:
        return None
    today = today or _dt.date.today()
    applied = applied_index() if applied is None else applied
    in_flight = in_flight_index() if in_flight is None else in_flight

    # Skip the row being asked about: a document must not cite itself. A manual
    # entry has no id and is therefore never the row in question.
    def _other(e):
        return job_id is None or e.get("id") is None or e["id"] != job_id

    hit = next((e for e in applied.get(key, []) if _other(e)), None)
    if hit:
        when = hit["applied"]
        # An application with no usable date still deserves a warning; it just
        # cannot carry an expiry. Silence would be the wrong failure here.
        until = when + _dt.timedelta(days=cooldown_days()) if when else None
        if until is None or until > today:
            return {
                "kind": "cooldown", "company": hit["company"],
                "applied": when.isoformat() if when else None,
                "until": until.isoformat() if until else None,
                "days_left": (until - today).days if until else None,
                "role": hit["role"], "note": hit["note"],
                "manual": hit.get("manual", False),
            }

    flight = in_flight.get(key)
    if flight and flight.get("id") != job_id:
        return {"kind": "in_queue", "company": flight["company"],
                "role": flight["role"], "status": flight["status"],
                "job_id": flight["id"]}
    return None


def line(flag: dict | None) -> str:
    """One-line rendering, shared by every surface so they cannot drift."""
    if not flag:
        return ""
    if flag["kind"] == "cooldown":
        src = "recorded by hand" if flag.get("manual") else "applied"
        if flag.get("until"):
            return (f"COOLDOWN - {src} to {flag['company']} on {flag['applied']}"
                    f" ({flag['role']}); next allowed {flag['until']}"
                    f", {flag['days_left']} day(s) left")
        return f"COOLDOWN - {src} to {flag['company']} (no date recorded)"
    return (f"IN YOUR QUEUE - {flag['company']} \"{flag['role']}\" is already "
            f"{flag['status']} and not yet applied")
