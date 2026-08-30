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

WHAT THIS PUBLISHES, AND WHY IT IS ENCRYPTED
--------------------------------------------
Everything. Every company, every tailored resume, every cover note, every
gap-honesty admission, every screening answer.

Network-edge auth is not enough on a static host. Cloudflare Access protects
the proxied hostname; it cannot protect the `*.vercel.app` domain Vercel assigns
automatically, which serves the same files, is public, and cannot be removed on
the Hobby plan. GitHub Pages has the same shape -- the published site is public
regardless of repository visibility.

So the payload is encrypted at rest with a passphrase you choose, AES-256-GCM
with a PBKDF2-SHA256 key. The browser asks for the passphrase and decrypts in
WebCrypto; the server only ever holds ciphertext. **That makes the host
irrelevant** -- a fully public URL leaks nothing without the passphrase, and the
same output can go to Vercel, Pages, S3 or anywhere else.

Cloudflare Access on top is still worth having. It stops a stranger reaching the
page at all, which is cheaper than relying on one secret. Defence in depth, not
either/or.

`noindex` ships three ways (meta tag, robots.txt, X-Robots-Tag). That is not
access control either; it stops the URL turning up in a search result.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import db
from .config import OUT_DIR, ROOT, TEMPLATE_DIR

# OWASP's floor for PBKDF2-SHA256 is 600k. The cost is paid once per unlock, in
# the browser, and a job hunt is worth more than the ~1s it adds on a phone.
PBKDF2_ITERATIONS = 600_000

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


def encrypt(plaintext: str, passphrase: str) -> dict:
    """AES-256-GCM under a PBKDF2-SHA256 key, in the shape WebCrypto expects.

    `cryptography` appends the GCM tag to the ciphertext, which is exactly what
    `crypto.subtle.decrypt` wants, so no splitting is needed on either side.
    """
    import base64
    import hashlib
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, PBKDF2_ITERATIONS, 32)
    ct = AESGCM(key).encrypt(iv, plaintext.encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()          # noqa: E731
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITERATIONS,
            "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}

_BANNER = """<div class="banner" style="border-left-color:var(--cool)">
      <b>Read-only mirror.</b> Exported {stamp}. Marking applied writes to the
      database, which only the local dashboard can reach &mdash; run
      <code>make review</code> to record one.
    </div>"""


def _rewrite(html: str, stamp: str) -> str:
    """Point the template at flat JSON and strip the write actions."""
    # One fetch of the ciphertext, decrypted in the browser, then the template's
    # own render path runs unchanged.
    html = html.replace(
        """const [jobs, stats] = await Promise.all([
    fetch('/api/queue').then(r=>r.json()),
    fetch('/api/stats').then(r=>r.json())
  ]);""",
        "const {jobs, stats} = await unlock();")

    # Drop the two POST buttons; keep "Open posting", which is the whole point
    # of reading this on a phone.
    html = html.replace(
        '''      <button class="did" data-act="applied" data-id="${j.id}">Mark applied</button>
      <button data-act="skip" data-id="${j.id}">Skip</button>
''', "")

    # The click handler now has nothing to bind to. Leaving a fetch() to a POST
    # route that does not exist would fail silently and look like a lost click.
    #
    # Removed by exact match, NOT by regex. A non-greedy `.*?\}\);` stops at the
    # FIRST `});` -- which is inside the handler, on the fetch line -- and leaves
    # its tail orphaned. That emits a SyntaxError and a blank page, while every
    # assertion about "/api/" still passes, because the fetch line did go.
    start = html.find("list.addEventListener('click'")
    if start != -1:
        end = html.find("\n});", start)
        if end == -1:
            raise RuntimeError(
                "dashboard.html's click handler no longer ends with '\\n});'. "
                "Fix this rewrite rather than shipping a live 'Mark applied'.")
        html = html[:start] + html[end + len("\n});\n"):]

    html = html.replace("<title>Review queue</title>",
                        '<title>Review queue</title>\n'
                        '<meta name="robots" content="noindex, nofollow">')
    html = html.replace("<main>", "<main>\n    " + _BANNER.format(stamp=stamp), 1)
    html = html.replace("</head>", _LOCK_CSS + "</head>", 1)
    html = html.replace("<body>", "<body>" + _LOCK_HTML, 1)
    html = html.replace("<script>", "<script>" + _UNLOCK_JS, 1)
    _assert_balanced(html)
    return html


def _assert_balanced(html: str) -> None:
    """Cheap structural check on the emitted script.

    Not a parser -- it ignores strings and comments, so it only catches gross
    damage. Gross damage is exactly what a text rewrite produces, and the
    alternative is finding out from a blank page on a phone.
    """
    body = html.split("<script>", 1)[-1].split("</script>", 1)[0]
    for open_c, close_c in (("{", "}"), ("(", ")"), ("[", "]")):
        if body.count(open_c) != body.count(close_c):
            raise RuntimeError(
                f"rewritten script is unbalanced: {body.count(open_c)} '{open_c}' "
                f"vs {body.count(close_c)} '{close_c}'. The export would emit a "
                f"SyntaxError and render a blank page.")


_LOCK_CSS = """<style>
 #lock{position:fixed;inset:0;background:#0F1319;display:grid;place-items:center;z-index:99}
 #lock form{width:min(90vw,22rem);text-align:center}
 #lock h2{font-family:var(--mono,monospace);font-size:12px;letter-spacing:.16em;
   text-transform:uppercase;color:#78899B;font-weight:700;margin:0 0 14px}
 #lock input{width:100%;padding:11px 13px;background:#161C25;color:#D9E1EA;
   border:1px solid #28323F;border-radius:3px;font:15px/1 var(--mono,monospace)}
 #lock input:focus{outline:none;border-color:#4FB3C9}
 #lock button{width:100%;margin-top:9px;padding:11px;border:0;border-radius:3px;
   background:#4FB3C9;color:#0F1319;font-weight:600;font-size:14px;cursor:pointer}
 #lock button:disabled{opacity:.55;cursor:default}
 #lockmsg{margin-top:11px;font:12px/1.5 var(--mono,monospace);color:#78899B;min-height:2.6em}
 #lockmsg.bad{color:#E8A33D}
</style>"""

_LOCK_HTML = """
<div id="lock"><form id="lockform">
  <h2>Review queue &mdash; locked</h2>
  <input id="pw" type="password" placeholder="passphrase" autocomplete="current-password"
         autocapitalize="off" autocorrect="off" spellcheck="false" required>
  <button id="lockgo" type="submit">Unlock</button>
  <div id="lockmsg">Decrypted in your browser. The server only ever holds ciphertext.</div>
</form></div>"""

# Mirrors site.encrypt(): PBKDF2-SHA256 -> AES-256-GCM, tag already appended.
_UNLOCK_JS = """
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));

async function decrypt(env, pass){
  const base = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(pass), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    {name:'PBKDF2', salt:b64(env.salt), iterations:env.iter, hash:'SHA-256'},
    base, {name:'AES-GCM', length:256}, false, ['decrypt']);
  const clear = await crypto.subtle.decrypt(
    {name:'AES-GCM', iv:b64(env.iv)}, key, b64(env.ct));
  return JSON.parse(new TextDecoder().decode(clear));
}

async function unlock(){
  const env = await fetch('payload.enc', {cache:'no-store'}).then(r=>r.json());
  const form = document.getElementById('lockform');
  const msg  = document.getElementById('lockmsg');
  const btn  = document.getElementById('lockgo');

  const tryPass = async pass => {
    const data = await decrypt(env, pass);
    sessionStorage.setItem('jobpipe-pass', pass);
    document.getElementById('lock').remove();
    return data;
  };

  // A refresh mid-review should not re-prompt. sessionStorage only: it dies
  // with the tab, and never reaches disk the way localStorage does.
  const remembered = sessionStorage.getItem('jobpipe-pass');
  if(remembered){
    try { return await tryPass(remembered); }
    catch(e){ sessionStorage.removeItem('jobpipe-pass'); }
  }

  return new Promise(resolve => {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      btn.disabled = true; msg.className = ''; msg.textContent = 'Deriving key\u2026';
      try {
        resolve(await tryPass(document.getElementById('pw').value));
      } catch(err){
        msg.className = 'bad';
        msg.textContent = 'Wrong passphrase.';
        btn.disabled = false;
        document.getElementById('pw').select();
      }
    });
  });
}
"""


def build(log=print, out_dir: Path | None = None,
          passphrase: str | None = None) -> Path:
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

    payload = json.dumps({"jobs": jobs, "stats": stats}, ensure_ascii=False)
    if passphrase:
        (site / "payload.enc").write_text(json.dumps(encrypt(payload, passphrase)))
        for stale in ("queue.json", "stats.json", "UNENCRYPTED-DO-NOT-HOST.json"):
            (site / stale).unlink(missing_ok=True)   # never leave plaintext behind
    else:
        # Unencrypted is for `python -m http.server` on localhost only. Named so
        # that finding it on a host is unambiguous rather than a shrug.
        (site / "payload.enc").unlink(missing_ok=True)
        (site / "UNENCRYPTED-DO-NOT-HOST.json").write_text(payload)
        log("  ! NO PASSPHRASE -- payload written in the clear. Do not deploy this.")
    (site / "index.html").write_text(
        _rewrite((TEMPLATE_DIR / "dashboard.html").read_text(), stamp))
    (site / "robots.txt").write_text(ROBOTS)
    (site / "vercel.json").write_text(json.dumps(VERCEL_JSON, indent=2))
    # Pages runs Jekyll by default and would skip anything underscore-prefixed.
    (site / ".nojekyll").write_text("")

    kb = sum(f.stat().st_size for f in site.iterdir() if f.is_file()) / 1024
    log(f"  {len(jobs)} job(s), {kb:.0f} kB -> {site}")
    log(f"  exported {stamp}"
        + ("  [AES-256-GCM, PBKDF2 x{:,}]".format(PBKDF2_ITERATIONS) if passphrase else ""))
    return site
