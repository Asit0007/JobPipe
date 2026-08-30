"""Static export of the review queue, for Vercel or GitHub Pages.

The live dashboard (`review_api.py`) needs a server and a writable SQLite file.
Neither exists on Vercel or Pages: Vercel's filesystem is ephemeral and Pages
serves static files only. So this exports the same queue as flat JSON and
rewrites the same template to read it -- one design to maintain, not two.

READ-ONLY, AND THAT IS DELIBERATE
---------------------------------
"Mark applied" is not exported. CLAUDE.md section 2 makes `review_api.mark_applied`
the single writer of `status='applied'`, and a static site cannot write anywhere
without a hosted database. Rather than fake the button, the export drops it and
says where the real one is. Applying still happens on the employer's site; the
status still gets recorded in the local dashboard.

WHAT THIS PUBLISHES
-------------------
Everything. Every company, every tailored resume, every cover note, every
gap-honesty admission, every screening answer. `review_api.py` binds 127.0.0.1
because it has no authentication of any kind, and this export does not add any.
**Put the hostname behind Cloudflare Access before the first visit.**

The output carries `noindex` in a meta tag, a robots.txt, and an
`X-Robots-Tag` header in vercel.json. That stops a search engine from indexing
it. It is not access control and is not a substitute for it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import db
from .config import OUT_DIR, ROOT, TEMPLATE_DIR

SITE_DIR = ROOT / "site"

# Vercel config: no build step, plus the headers a page like this should carry.
VERCEL_JSON = {
    "$schema": "https://openapi.vercel.sh/vercel.json",
    "cleanUrls": True,
    "headers": [{
        "source": "/(.*)",
        "headers": [
            {"key": "X-Robots-Tag",
             "value": "noindex, nofollow, noarchive, nosnippet, noimageindex"},
            {"key": "X-Content-Type-Options", "value": "nosniff"},
            {"key": "X-Frame-Options", "value": "DENY"},
            {"key": "Referrer-Policy", "value": "no-referrer"},
            # The queue changes whenever the pipeline runs; never serve a stale one.
            {"key": "Cache-Control", "value": "no-store, max-age=0"},
        ],
    }],
}

ROBOTS = "User-agent: *\nDisallow: /\n"

_BANNER = """<div class="banner" style="border-left-color:var(--cool)">
      <b>Read-only mirror.</b> Exported {stamp}. Marking applied writes to the
      database, which only the local dashboard can reach &mdash; run
      <code>make review</code> to record one.
    </div>"""


def _rewrite(html: str, stamp: str) -> str:
    """Point the template at flat JSON and strip the write actions."""
    html = html.replace("fetch('/api/queue')", "fetch('queue.json')")
    html = html.replace("fetch('/api/stats')", "fetch('stats.json')")

    # Drop the two POST buttons; keep "Open posting", which is the whole point
    # of reading this on a phone.
    html = html.replace(
        '''      <button class="did" data-act="applied" data-id="${j.id}">Mark applied</button>
      <button data-act="skip" data-id="${j.id}">Skip</button>
''', "")

    # The click handler now has nothing to bind to. Leaving a fetch() to a POST
    # route that does not exist would fail silently and look like a lost click.
    html = re.sub(
        r"list\.addEventListener\('click'.*?\}\);\n", "", html, flags=re.S)

    html = html.replace("<title>Review queue</title>",
                        '<title>Review queue</title>\n'
                        '<meta name="robots" content="noindex, nofollow">')
    html = html.replace("<main>", "<main>\n    " + _BANNER.format(stamp=stamp), 1)
    if "Read-only mirror" not in html:      # template has no bare <main>
        html = html.replace('<main class="', '<main data-ro class="', 1)
    return html


def build(log=print, out_dir: Path | None = None) -> Path:
    """Write the static site. Returns its directory."""
    from datetime import datetime, timezone

    # Idempotent, and cheap. Without it a fresh checkout dies with
    # "no such table: jobs" -- the same failure `cli status` once had.
    db.init()

    site = out_dir or SITE_DIR
    site.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    jobs = []
    for status in ("queued", "prepared"):
        for r in db.fetch(status=status, limit=200):
            prep = ""
            path = r["resume_path"] and Path(r["resume_path"])
            if path and path.exists():
                prep = path.read_text()
            elif path:
                # resume_path is absolute and points at the machine that ran
                # prepare. On a re-export elsewhere, fall back to out/ by id.
                guess = next(OUT_DIR.glob(f"{r['id']:05d}_*.md"), None)
                prep = guess.read_text() if guess else ""
            jobs.append({
                "id": r["id"], "title": r["title"], "company": r["company"],
                "location": r["location"], "score": r["score"],
                "reason": r["score_reason"], "url": r["apply_url"] or r["url"],
                "missing": json.loads(r["missing_skills"] or "[]"),
                "flags": json.loads(r["red_flags"] or "[]"),
                "source": r["source"], "prep": prep,
            })
    jobs.sort(key=lambda x: x["score"] or 0, reverse=True)

    with db.connect() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
    stats = {"by_status": {r["status"]: r["n"] for r in rows},
             "gemini_left": "-", "exported_at": stamp}

    (site / "queue.json").write_text(json.dumps(jobs, indent=1, ensure_ascii=False))
    (site / "stats.json").write_text(json.dumps(stats, indent=1))
    (site / "index.html").write_text(
        _rewrite((TEMPLATE_DIR / "dashboard.html").read_text(), stamp))
    (site / "robots.txt").write_text(ROBOTS)
    (site / "vercel.json").write_text(json.dumps(VERCEL_JSON, indent=2))
    # Pages runs Jekyll by default and would skip anything underscore-prefixed.
    (site / ".nojekyll").write_text("")

    kb = sum(f.stat().st_size for f in site.iterdir() if f.is_file()) / 1024
    log(f"  {len(jobs)} job(s), {kb:.0f} kB -> {site}")
    log(f"  exported {stamp}")
    return site
