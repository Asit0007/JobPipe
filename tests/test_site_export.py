"""The static export, for hosting the dashboard on Vercel or GitHub Pages.

Neither can run `review_api.py`: it needs a server and a writable SQLite file,
and Vercel's filesystem is ephemeral while Pages serves static files only. The
export reuses the same template so there is one design, not two.

What these pin is the safety-critical half. The export publishes every company,
every tailored resume, every cover note and every screening answer, and adds no
authentication -- so the write actions must be gone (a static site cannot honour
them, and a button that silently does nothing reads as a lost click) and the
noindex markers must be present.
"""
import json

import pytest

from jobpipe import site


def test_the_rewritten_script_is_syntactically_intact():
    """The click handler was first removed with a non-greedy regex, which stopped
    at the FIRST `});` -- inside the handler, on the fetch line -- and orphaned
    its tail. That emits a SyntaxError and a blank page, while every assertion
    about "/api/" still passed, because the fetch line did go. Structure has to
    be checked directly."""
    body = site._rewrite(_TEMPLATE, "x").split("<script>", 1)[1].split("</script>", 1)[0]
    for o, c in (("{", "}"), ("(", ")"), ("[", "]")):
        assert body.count(o) == body.count(c), f"unbalanced {o}{c}"
    real = site._rewrite((site.TEMPLATE_DIR / "dashboard.html").read_text(), "x")
    rbody = real.split("<script>", 1)[1].split("</script>", 1)[0]
    for o, c in (("{", "}"), ("(", ")"), ("[", "]")):
        assert rbody.count(o) == rbody.count(c), f"real template unbalanced {o}{c}"


def test_a_template_whose_handler_changed_shape_fails_loudly():
    """Better a build error than a blank page on a phone."""
    # Reformat the handler's terminator so the exact anchor no longer matches.
    broken = _TEMPLATE.replace("\n});\n\nload();", "\n} ) ;\n\nload();")
    with pytest.raises(RuntimeError, match="unbalanced|click handler"):
        site._rewrite(broken, "x")


def test_the_write_actions_are_removed():
    """`review_api.mark_applied` is the only writer of status='applied'
    (CLAUDE.md section 2). A static mirror cannot write anywhere, so faking the
    button would either lie or silently drop the click."""
    html = site._rewrite(_TEMPLATE, "2026-01-01 00:00 UTC")
    assert "Mark applied" not in html
    assert "data-act" not in html
    assert "addEventListener('click'" not in html


def test_the_apply_link_survives():
    """Opening the posting is the whole reason to read this on a phone."""
    assert "Open posting" in site._rewrite(_TEMPLATE, "x")


def test_the_page_no_longer_calls_the_live_api():
    html = site._rewrite(_TEMPLATE, "x")
    assert "/api/" not in html


def test_the_page_says_it_is_read_only_and_where_the_real_one_is():
    html = site._rewrite(_TEMPLATE, "2026-01-01 00:00 UTC")
    assert "Read-only mirror" in html
    assert "2026-01-01 00:00 UTC" in html, "a stale mirror must say when it was made"
    assert "make review" in html


def test_search_engines_are_told_to_stay_out():
    """Not access control -- that is Cloudflare Access's job -- but a job hunt
    turning up in Google while employed is its own failure."""
    assert 'name="robots" content="noindex, nofollow"' in site._rewrite(_TEMPLATE, "x")
    assert site.ROBOTS.strip().endswith("Disallow: /")
    hdrs = {h["key"]: h["value"] for h in site.VERCEL_JSON["headers"][0]["headers"]}
    assert "noindex" in hdrs["X-Robots-Tag"]
    assert hdrs["X-Frame-Options"] == "DENY"
    assert "no-store" in hdrs["Cache-Control"], "a cached queue is a wrong queue"


def test_build_writes_every_file_a_host_needs(tmp_path):
    out = site.build(log=lambda *a: None, out_dir=tmp_path, passphrase="pw")
    for name in ("index.html", "payload.enc", "robots.txt", "vercel.json", ".nojekyll"):
        assert (out / name).exists(), f"{name} missing"
    json.loads((out / "payload.enc").read_text())
    json.loads((out / "vercel.json").read_text())


# --------------------------------------------------------------------------
# encryption -- what makes the host irrelevant
# --------------------------------------------------------------------------
def test_the_payload_is_encrypted_at_rest(tmp_path):
    """Cloudflare Access covers the proxied hostname. It cannot cover the
    *.vercel.app domain Vercel assigns and will not remove on Hobby, and a
    GitHub Pages URL is public regardless of repo visibility. Encrypting the
    payload is what makes a public URL harmless."""
    out = site.build(log=lambda *a: None, out_dir=tmp_path, passphrase="s3cret")
    blob = (out / "payload.enc").read_text()
    assert "resume" not in blob.lower() and "company" not in blob.lower()
    env = json.loads(blob)
    assert set(env) == {"v", "kdf", "iter", "salt", "iv", "ct"}
    assert env["iter"] >= 600_000, "below the OWASP floor for PBKDF2-SHA256"


def test_encryption_round_trips_and_rejects_a_wrong_passphrase():
    import base64
    import hashlib

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    env = site.encrypt('{"x":1}', "right")
    d = lambda k: base64.b64decode(env[k])                       # noqa: E731
    key = hashlib.pbkdf2_hmac("sha256", b"right", d("salt"), env["iter"], 32)
    assert AESGCM(key).decrypt(d("iv"), d("ct"), None) == b'{"x":1}'

    bad = hashlib.pbkdf2_hmac("sha256", b"wrong", d("salt"), env["iter"], 32)
    with pytest.raises(Exception):
        AESGCM(bad).decrypt(d("iv"), d("ct"), None)


def test_each_export_uses_a_fresh_salt_and_iv():
    """Reusing a GCM nonce across exports of overlapping data is a real break,
    not a theoretical one."""
    a, b = site.encrypt("x", "pw"), site.encrypt("x", "pw")
    assert a["salt"] != b["salt"] and a["iv"] != b["iv"] and a["ct"] != b["ct"]


def test_a_blank_passphrase_exits_non_zero_so_the_deploy_chain_stops(tmp_path, monkeypatch):
    """`make deploy` is `cli site && cd site && vercel --prod`. Returning 0 on a
    blank passphrase let the chain continue and upload the PLAINTEXT payload.
    Naming the file UNENCRYPTED-DO-NOT-HOST.json was meant to be the safeguard;
    a filename is not a safeguard when the next command uploads the directory
    regardless."""
    import sys as _sys

    from jobpipe import cli
    monkeypatch.setenv("JOBPIPE_SITE_PASSPHRASE", "")
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(site, "SITE_DIR", tmp_path)
    with pytest.raises(SystemExit) as e:
        cli.cmd_site()
    assert e.value.code != 0, "a zero exit here lets vercel upload the plaintext"


def test_without_a_passphrase_the_plaintext_file_is_named_so_nobody_hosts_it(tmp_path):
    out = site.build(log=lambda *a: None, out_dir=tmp_path)
    assert (out / "UNENCRYPTED-DO-NOT-HOST.json").exists()
    assert not (out / "payload.enc").exists()


def test_switching_to_a_passphrase_removes_the_plaintext(tmp_path):
    """A stale plaintext file left beside the ciphertext would be the whole
    point defeated, and it would be invisible."""
    site.build(log=lambda *a: None, out_dir=tmp_path)
    site.build(log=lambda *a: None, out_dir=tmp_path, passphrase="pw")
    assert not (tmp_path / "UNENCRYPTED-DO-NOT-HOST.json").exists()
    assert not (tmp_path / "queue.json").exists()


def test_the_unlock_gate_is_wired_into_the_page():
    html = site._rewrite(_TEMPLATE, "x")
    assert "id=\"lock\"" in html and "unlock()" in html
    assert "PBKDF2" in html and "AES-GCM" in html
    assert "fetch('payload.enc'" in html
    assert "sessionStorage.setItem" in html
    assert "localStorage.setItem" not in html, "must not persist past the tab"


def test_the_export_never_lands_inside_the_repo_history():
    """It is the entire job hunt in one folder, and the repo is public."""
    ignored = (site.ROOT / ".gitignore").read_text().splitlines()
    assert "site/" in [l.strip() for l in ignored]


# The two lines the rewrite has to find. Kept here so a template edit that
# breaks the export fails loudly instead of shipping a live "Mark applied".
_TEMPLATE = """<!DOCTYPE html><html><head><title>Review queue</title></head>
<body><main>
    <div class="actions">
      <a class="btn btn--go" href="${j.url}" target="_blank" rel="noopener">Open posting</a>
      <button class="did" data-act="applied" data-id="${j.id}">Mark applied</button>
      <button data-act="skip" data-id="${j.id}">Skip</button>
    </div>
<script>
async function load(){
  const [jobs, stats] = await Promise.all([
    fetch('/api/queue').then(r=>r.json()),
    fetch('/api/stats').then(r=>r.json())
  ]);
}

list.addEventListener('click', async e => {
  const b = e.target.closest('button[data-act]');
  await fetch(`/api/jobs/${b.dataset.id}/${b.dataset.act}`, {method:'POST'});
});

load();
</script></main></body></html>"""


def test_the_real_template_still_matches_what_the_rewrite_looks_for():
    """If someone edits templates/dashboard.html, the export must not silently
    stop stripping the buttons."""
    real = (site.TEMPLATE_DIR / "dashboard.html").read_text()
    out = site._rewrite(real, "x")
    assert "Mark applied" not in out and "/api/" not in out


# --------------------------------------------------------------------------
# readme-stats -- the README is public, the job hunt is not
# --------------------------------------------------------------------------
def test_readme_stats_publishes_counts_and_never_the_job_hunt(tmp_path, monkeypatch, capsys):
    """CLAUDE.md section 3: the repo is public, the search is not. Aggregate
    funnel counts are the portfolio value and are already published; company
    names, titles, locations and the `applied` count describe the job hunt and
    must never be written into a tracked file."""
    from jobpipe import cli, db

    readme = tmp_path / "README.md"
    readme.write_text("intro\n\n| stage | count | |\n|---|---:|---|\n| ingested | 1 | x |\n\ntail\n")
    monkeypatch.setattr(cli.__dict__["db"], "connect", db.connect)
    import jobpipe.config as cfg
    monkeypatch.setattr(cfg, "ROOT", tmp_path)

    db.init()
    db.upsert_job({"fingerprint": "fp-readme-1", "source": "greenhouse", "source_id": "1",
                   "company": "Wildly Distinctive Corp", "company_canonical": "wildly",
                   "title": "Senior Widget Polisher", "location": "Atlantis",
                   "url": "https://example.invalid/1", "apply_url": "", "description": "widgets",
                   "salary_raw": "", "posted_at": "", "remote": 0})
    cli.cmd_readme_stats()

    out = readme.read_text()
    assert "| ingested |" in out and "funnel:start" in out
    for secret in ("Wildly Distinctive Corp", "Senior Widget Polisher", "Atlantis",
                   "example.invalid", "applied"):
        assert secret not in out, f"{secret!r} reached a tracked file"
