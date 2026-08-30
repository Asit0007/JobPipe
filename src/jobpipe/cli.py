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


def cmd_tex():
    """Rebuild the .tex (and .json) for prepared jobs. `cli tex [job_id|all]`

    Costs nothing: the payload is stored beside the markdown, so this never
    calls a model. Documents prepared before the sidecar existed are recovered
    from the markdown itself, and the job description is re-read from the DB so
    the skill rows can still be ordered against the JD.
    """
    from . import render
    target = sys.argv[2] if len(sys.argv) > 2 else "all"
    docs = render.documents(target)
    if not docs:
        print(f"no prepared documents matching {target!r} in out/")
        return
    from . import tailor
    for md in docs:
        doc = render.load(md)
        _refresh_job(doc)
        if not doc.get("bullets"):
            print(f"  ! {md.name}: no sourceable bullets, skipped")
            continue

        # Re-run the gate against the CURRENT rules. A fix to claims.py should
        # reach documents already on disk without spending 2 of the tailor
        # model's 20 daily calls to re-tailor them.
        unsourced = (doc.get("flags") or {}).get("unsourced")
        doc["bullets"], doc["flags"] = render.gate(doc["bullets"], doc)
        if unsourced:
            doc["flags"]["unsourced"] = unsourced

        render.write_sidecar(doc, md)
        tex = render.write_tex(doc, md)
        md.write_text(tailor._render(doc["job"], doc, doc["bullets"], [],
                                     doc.get("screening"), doc["flags"]))
        flags = doc["flags"]
        bits = []
        if flags.get("dropped"):
            bits.append(f"{len(flags['dropped'])} dropped")
        if flags.get("drift"):
            bits.append(f"{len(flags['drift'])} to check")
        if flags.get("prose"):
            bits.append(f"prose: {flags['prose'][0]['field']}")
        if unsourced:
            bits.append(f"{len(unsourced)} unsourced")
        note = f"  ({', '.join(bits)})" if bits else ""
        print(f"  {tex.name}  {len(doc['bullets'])} bullets{note}")
    print(f"\n{len(docs)} document(s). Compile with: make pdf")


def cmd_site():
    """Export the queue as a static site. `cli site`

    For hosting the dashboard on Vercel or GitHub Pages, neither of which can
    run `review_api.py` -- it needs a server and a writable SQLite file. The
    export is READ-ONLY by construction: marking applied is a database write and
    section 2 keeps `review_api.mark_applied` as its only writer.

    It publishes the entire job hunt and adds no authentication. Put the
    hostname behind Cloudflare Access before the first visit.
    """
    import getpass
    import os

    from . import site

    # Network-edge auth cannot cover the *.vercel.app domain Vercel assigns and
    # will not remove on Hobby, nor a GitHub Pages URL. Encrypting the payload
    # makes the host irrelevant instead of trusting it.
    pw = os.getenv("JOBPIPE_SITE_PASSPHRASE") or ""
    if not pw and sys.stdin.isatty():
        pw = getpass.getpass("Passphrase to encrypt the export (blank = local only): ")
        if pw and pw != getpass.getpass("Confirm: "):
            print("  passphrases did not match")
            sys.exit(1)
    out = site.build(passphrase=pw or None)
    if not pw:
        return
    print("\n  Deploy (the data never enters git, and the host only sees ciphertext):")
    print(f"     cd {out} && vercel --prod")
    print("  Keep the passphrase in a password manager. Losing it means re-exporting,")
    print("  not recovering -- there is no reset.")


def cmd_rescreen():
    """Refresh screening answers on prepared docs. `cli rescreen [job_id|all]`

    Costs ONE call per job, against the two that re-running `prepare` would
    spend -- and it leaves the tailored bullets alone, so a document already
    reviewed stays valid. Use it when facts.yaml gains a fact the answers should
    reflect, which is what happened when the work-authorisation fact landed and
    every prepared document was still saying "my candidate profile does not
    specify my work authorization status".
    """
    from . import render, screening, tailor
    target = sys.argv[2] if len(sys.argv) > 2 else "all"
    docs = render.documents(target)
    if not docs:
        print(f"no prepared documents matching {target!r} in out/")
        return
    done = 0
    for md in docs:
        doc = render.load(md)
        _refresh_job(doc)
        try:
            doc["screening"] = screening.generate_for(doc["job"])
        except Exception as e:
            print(f"  ! {md.name}: {type(e).__name__}: {str(e)[:90]}")
            break
        doc["bullets"], doc["flags"] = render.gate(doc["bullets"], doc)
        render.write_sidecar(doc, md)
        render.write_tex(doc, md)
        md.write_text(tailor._render(doc["job"], doc, doc["bullets"], [],
                                     doc["screening"], doc["flags"]))
        weak = sum(1 for a in (doc["screening"].get("answers") or [])
                   if a.get("confidence") == "low")
        print(f"  {md.stem}  {len(doc['screening'].get('answers') or [])} answers"
              + (f", {weak} still weak" if weak else ""))
        done += 1
    print(f"\n{done} document(s) refreshed")


def cmd_pdf():
    """Compile prepared .tex files to PDF. `cli pdf [job_id|all]`"""
    from . import render
    engine = render.find_engine()
    if not engine:
        print("no TeX engine found on PATH.\n"
              "  brew install tectonic     # single binary, fetches only what it needs\n"
              "  brew install --cask mactex-no-gui   # the full distribution, ~5 GB\n"
              "Until then the .tex files are ready to compile anywhere, "
              "including overleaf.com.")
        sys.exit(1)

    target = sys.argv[2] if len(sys.argv) > 2 else "all"
    docs = render.documents(target)
    made, failed = 0, 0
    for md in docs:
        tex = md.with_suffix(".tex")
        if not tex.exists():
            print(f"  ! {tex.name} missing -- run `cli tex` first")
            failed += 1
            continue
        ok, detail, pages = render.compile_pdf(tex, engine)
        if not ok:
            print(f"  ! {tex.name}: {detail}")
            failed += 1
            continue
        # The template's checklist asks for one page. The engine already counted,
        # so answer it rather than leaving a box to eyeball.
        # Two pages is the expected shape now that the skill rows carry the
        # master resume's full inventory. Three is a signal to cut.
        note = "" if pages in (None, 1) else f"  ({pages} pages)"
        if pages and pages > 2:
            note += "  <- over two pages, trim"
        print(f"  {tex.with_suffix('.pdf').name}{note}")
        made += 1
    print(f"\n{made} PDF(s) written by {engine}" + (f", {failed} failed" if failed else ""))
    if failed:
        sys.exit(1)


def _refresh_job(doc: dict) -> None:
    """Put the live job row back on a payload recovered from markdown.

    The description is what orders the skill rows and picks the tagline
    keywords, and the markdown never carried it.
    """
    job_id = (doc.get("job") or {}).get("id")
    if not job_id:
        return
    with db.connect() as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row:
        doc["job"].update({k: row[k] for k in row.keys() if k in doc["job"] and row[k]})


def cmd_claims():
    """Show what the never_claim gate is actually looking for.

    The rules in facts.yaml are prose, so the terms are extracted heuristically.
    Read this before trusting the gate -- if a rule is not producing the term
    you meant, reword it in facts.yaml.
    """
    from .claims import never_claim_terms, tech_vocab
    from .config import facts
    cfg = facts()
    by_rule: dict[str, list[str]] = {}
    for term, rule in never_claim_terms(cfg):
        by_rule.setdefault(rule, []).append(term)
    print("never_claim -- a bullet introducing one of these is DROPPED\n")
    for rule, terms in by_rule.items():
        print(f"  {rule[:88]}")
        print(f"      -> {', '.join(terms)}")
    silent = [r for r in (cfg.get("never_claim") or []) if r not in by_rule]
    if silent:
        print(f"\n{len(silent)} rule(s) produced NO term and are not enforced:")
        for r in silent:
            print(f"  - {r[:88]}")
    print(f"\ndrift vocabulary -- introducing one of these is FLAGGED, not dropped\n"
          f"  {', '.join(tech_vocab(cfg))}")


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
