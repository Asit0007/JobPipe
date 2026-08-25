"""Entry points. `python -m jobpipe.cli <stage>`"""
from __future__ import annotations

import sys

from . import db
from .config import companies


def cmd_init():
    db.init()
    print(f"db ready at {db.DB_PATH if hasattr(db,'DB_PATH') else ''}")


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
    db.init()
    from .sources import ALL_SOURCES
    total = {"inserted": 0, "seen": 0}
    for name, fetch in ALL_SOURCES.items():
        print(f"{name}:")
        try:
            jobs = fetch()
        except Exception as e:
            print(f"  {type(e).__name__}: {e} -- skipping source")
            continue
        for job in jobs:
            total[db.upsert_job(job)] += 1
    print(f"\n{total['inserted']} new, {total['seen']} already known")
    db.log_run("ingest", True, f"{total['inserted']} new")


def cmd_score():
    from .score import run
    run()


def cmd_prepare():
    from .tailor import run
    run()


def cmd_notify():
    from .notify import run
    run()


def cmd_track():
    from .track import run
    run()


def cmd_status():
    from .llm import budget_remaining
    with db.connect() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) n FROM jobs GROUP BY status ORDER BY n DESC"
        ).fetchall()
    print("pipeline state")
    for r in rows:
        print(f"  {r['status']:<14} {r['n']}")
    print(f"\ngemini calls left today: {budget_remaining()}")


COMMANDS = {k[4:].replace("_", "-"): v for k, v in list(globals().items()) if k.startswith("cmd_")}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: python -m jobpipe.cli <" + " | ".join(COMMANDS) + ">")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
