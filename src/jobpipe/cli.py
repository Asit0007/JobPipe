"""Entry points. `python -m jobpipe.cli <stage>`"""
from __future__ import annotations

import sys

from . import db
from .config import DB_PATH, ConfigError, companies, profile
from .normalize import AUTHORITATIVE_SOURCES, canon_company, is_near_duplicate


def cmd_init():
    print(f"db ready at {DB_PATH}")


def cmd_verify_sources():
    from .sources import ashby, greenhouse, lever
    checkers = {"greenhouse": greenhouse.verify, "lever": lever.verify, "ashby": ashby.verify}
    dead = []
    for ats, check in checkers.items():
        for slug in companies().get(ats, []) or []:
            ok = check(slug)
            print(f"  {'OK  ' if ok else 'DEAD'} {ats}/{slug}")
            if not ok:
                dead.append(f"{ats}/{slug}")
    if dead:
        print(f"\n{len(dead)} dead slug(s). Remove or fix them in config/companies.yaml:")
        for d in dead:
            print(f"  - {d}")
    else:
        print("\nall slugs live")


def cmd_ingest():
    from .sources import ALL_SOURCES

    total = {"inserted": 0, "enriched": 0, "seen": 0, "near_dupe": 0}
    for name, fetch in ALL_SOURCES.items():
        print(f"{name}:")
        try:
            jobs = fetch()
        except Exception as e:
            print(f"  {type(e).__name__}: {e} -- skipping source")
            continue
        for job in jobs:
            # The fingerprint catches exact repeats. It does not catch a
            # consultancy reposting the same requisition with the title reworded,
            # which is most of the noise in this market -- and every one of those
            # costs an LLM call. Compare against the same company's recent rows
            # before inserting.
            if _is_repost(job):
                total["near_dupe"] += 1
                continue
            total[db.upsert_job(job)] += 1

    print(f"\n{total['inserted']} new, {total['enriched']} enriched, "
          f"{total['seen']} already known, {total['near_dupe']} near-duplicate reposts skipped")
    db.log_run("ingest", True, f"{total['inserted']} new / {total['near_dupe']} dupes")


# The fuzzy check only earns its keep on aggregators and email alerts, where the
# same job really does arrive three times under three titles. Running it over
# ATS rows costs real postings -- measured against a live board it collapsed
# "Senior Channel Partner Manager" into "Channel Partner Manager".
def _is_repost(job: dict) -> bool:
    if job["source"] in AUTHORITATIVE_SOURCES:
        return False
    for row in db.recent_by_company(job["company_canonical"], within_days=30):
        if row["title"] == job["title"]:
            continue          # exact match is the fingerprint's job, not ours
        if is_near_duplicate(job, {"company": row["company"], "title": row["title"],
                                   "location": row["location"]}):
            return True
    return False


def cmd_fetch_jd():
    from . import jdfetch
    jdfetch.run()


def cmd_score():
    from .score import run
    run()


def cmd_prepare():
    from .tailor import run
    run()


def cmd_screening():
    """Screening answers for one job, printed. `cli.py screening <job_id>`

    prepare already appends these to the markdown; this is for when you want
    them for a role you found yourself, outside the pipeline.
    """
    from . import screening
    if len(sys.argv) < 3:
        print("usage: python -m jobpipe.cli screening <job_id>")
        sys.exit(1)
    with db.connect() as c:
        job = c.execute("SELECT * FROM jobs WHERE id = ?", (sys.argv[2],)).fetchone()
    if not job:
        print(f"no job with id {sys.argv[2]}")
        sys.exit(1)
    print(screening.render(screening.generate_for(job)))


def cmd_notify():
    from .notify import run
    run()


def cmd_track():
    from .track import run
    run()


def cmd_status():
    from .llm import budget_by_model, budget_remaining
    with db.connect() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) n FROM jobs GROUP BY status ORDER BY n DESC"
        ).fetchall()
    print("pipeline state")
    for r in rows:
        print(f"  {r['status']:<14} {r['n']}")
    if not rows:
        print("  (empty -- run ingest)")
    # Per model, because the quota is per model: 500/day each, not shared.
    per_model = budget_by_model()
    if per_model:
        print("\ngemini calls left today")
        for name, left in per_model.items():
            print(f"  {name:<28} {left}")
    else:
        print(f"\ngemini calls left today: {budget_remaining()} per model (none used yet)")

    stale = profile()["thresholds"].get("stale_after_days")
    if stale:
        print(f"postings are archived after {stale} days without a sighting")


COMMANDS = {k[4:].replace("_", "-"): v for k, v in list(globals().items()) if k.startswith("cmd_")}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: python -m jobpipe.cli <" + " | ".join(COMMANDS) + ">")
        sys.exit(1)
    # Idempotent, and cheap. Every command below either reads or writes the DB,
    # and `status` on a fresh checkout used to crash with "no such table".
    db.init()
    try:
        COMMANDS[sys.argv[1]]()
    except ConfigError as e:
        print(f"\nconfig error:\n{e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
