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

from jobpipe import site


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


def test_the_fetches_point_at_flat_json():
    html = site._rewrite(_TEMPLATE, "x")
    assert "fetch('queue.json')" in html and "fetch('stats.json')" in html
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
    out = site.build(log=lambda *a: None, out_dir=tmp_path)
    for name in ("index.html", "queue.json", "stats.json", "robots.txt",
                 "vercel.json", ".nojekyll"):
        assert (out / name).exists(), f"{name} missing"
    json.loads((out / "queue.json").read_text())
    json.loads((out / "vercel.json").read_text())


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
