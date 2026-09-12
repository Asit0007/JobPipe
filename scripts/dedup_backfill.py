#!/usr/bin/env python3
"""Re-fingerprint the corpus after canon_place(), and retire the duplicates.

Until 2026-09-12 `fingerprint()` hashed the raw canonical location, so one
requisition reaching us as "Pune", "Pune, Maharashtra" and "Pune Division" from
three boards became three rows. `canon_place()` fixes that going forward; this
fixes what is already stored.

Two things make it delicate, both learned from the 7.55 backfill:

  * `fingerprint` is TEXT UNIQUE NOT NULL, so a duplicate cannot simply be
    recomputed -- the update raises. Losers therefore KEEP their stale hash on
    purpose. Nothing will ever match it again, so future sightings land on the
    row that owns the clean one, and archive_stale() retires the orphan.
  * Deleting is wrong. A loser may be `applied`, or hold documents on disk;
    deleting the row orphans its .md/.tex/.json/.pdf silently.

Dry run by default. `--apply` writes, in ONE transaction.
"""
import argparse
import pathlib
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from jobpipe.config import DB_PATH                       # noqa: E402
from jobpipe.normalize import fingerprint                # noqa: E402

# Higher wins. A status a HUMAN set outranks any the pipeline set: keeping a
# `shortlisted` row over the `skipped` twin would re-prepare a job already
# declined, and over an `applied` twin would spend tailor budget applying twice.
RANK = {"applied": 8, "queued": 7, "prepared": 6, "skipped": 5,
        "shortlisted": 4, "scored": 3, "filtered": 2, "discovered": 1, "stale": 0}

# Retiring one of these costs nothing and stops it consuming tailor budget.
# Anything else the human has touched keeps its status; it only gets a note.
RETIRABLE = {"discovered", "scored", "shortlisted"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, company, title, location, source, status, fingerprint, notes "
        "FROM jobs").fetchall()
    print(f"{len(rows)} rows in {args.db}")

    groups = defaultdict(list)
    for r in rows:
        groups[fingerprint(r["company"], r["title"], r["location"] or "")].append(r)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    refp = [(r, k) for k, v in groups.items() for r in v
            if len(v) == 1 and r["fingerprint"] != k]

    print(f"{len(dupes)} duplicate group(s), "
          f"{sum(len(v) - 1 for v in dupes.values())} row(s) to retire")
    print(f"{len(refp)} unique row(s) whose fingerprint just changes\n")

    plan_fp, plan_note, plan_stale, plan_retire = [], [], [], []
    for key, members in sorted(dupes.items()):
        members = sorted(members, key=lambda r: (-RANK.get(r["status"], 0), r["id"]))
        keep, losers = members[0], members[1:]
        if keep["fingerprint"] != key:
            plan_fp.append((keep["id"], key))
        surfaced = any(r["status"] in RANK and RANK[r["status"]] >= 4 for r in members)
        if surfaced:
            print(f"  keep #{keep['id']:<6} {keep['status']:<11} "
                  f"{keep['company'][:24]:<24} {(keep['location'] or '')[:22]:<22} {keep['source']}")
        for lo in losers:
            note = (f"duplicate of #{keep['id']} (same company/title/city); "
                    f"fingerprint retired by dedup_backfill 2026-09-12")
            plan_note.append((lo["id"], note))
            plan_retire.append(lo["id"])
            if lo["status"] in RETIRABLE:
                plan_stale.append(lo["id"])
            if surfaced:
                mark = "-> stale" if lo["status"] in RETIRABLE else "status kept"
                print(f"    dup #{lo['id']:<6} {lo['status']:<11} "
                      f"{(lo['location'] or '')[:22]:<22} {lo['source']:<18} {mark}")

    print(f"\nplan: {len(plan_fp) + len(refp)} fingerprint update(s), "
          f"{len(plan_retire)} retired to dup:<id>, "
          f"{len(plan_note)} note(s), {len(plan_stale)} row(s) -> stale")
    if not args.apply:
        print("\nDRY RUN -- rerun with --apply to write")
        return 0

    cur = db.cursor()
    try:
        cur.execute("BEGIN")
        # Two passes. A row's NEW fingerprint may be another row's CURRENT one,
        # so writing finals directly can collide mid-transaction even though the
        # end state is unique. Park everything on a guaranteed-free value first.
        updates = plan_fp + [(r["id"], k) for r, k in refp]
        # A loser keeps no usable hash: its stored one is stale AND may be the
        # very value some other group's keeper is about to claim, which is what
        # made the first attempt raise. Give it an explicit retired marker
        # instead -- guaranteed unique, never equal to any fingerprint(), and
        # legible in the table as "this row lost a dedup" rather than looking
        # like a live hash that simply stopped matching.
        for jid in plan_retire:
            cur.execute("UPDATE jobs SET fingerprint = ? WHERE id = ?", (f"dup:{jid}", jid))
        for jid, _ in updates:
            cur.execute("UPDATE jobs SET fingerprint = ? WHERE id = ?", (f"tmp:{jid}", jid))
        for jid, key in updates:
            cur.execute("UPDATE jobs SET fingerprint = ? WHERE id = ?", (key, jid))
        for jid, note in plan_note:
            cur.execute(
                "UPDATE jobs SET notes = COALESCE(NULLIF(notes,'') || char(10), '') || ? "
                "WHERE id = ?", (note, jid))
        for jid in plan_stale:
            cur.execute("UPDATE jobs SET status = 'stale' WHERE id = ?", (jid,))
        db.commit()
    except Exception:
        db.rollback()
        raise

    bad = db.execute("SELECT COUNT(*) FROM jobs WHERE fingerprint LIKE 'tmp:%'").fetchone()[0]
    dup = db.execute("SELECT COUNT(*) FROM (SELECT fingerprint FROM jobs "
                     "GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0]
    print(f"\napplied. leftover tmp fingerprints: {bad}   duplicate fingerprints: {dup}")
    print("integrity:", db.execute("PRAGMA integrity_check").fetchone()[0])
    return 0 if bad == 0 and dup == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
