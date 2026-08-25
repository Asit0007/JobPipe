from __future__ import annotations

import re

import httpx

from ..normalize import fingerprint

UA = {"User-Agent": "jobpipe/0.1 (personal job search tool)"}


def strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</(p|div|li|h\d)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def make_job(*, source, source_id, company, title, location, url,
             description="", apply_url=None, salary_raw=None, posted_at=None) -> dict:
    from ..normalize import canon_company
    loc = location or ""
    return {
        "fingerprint": fingerprint(company, title, loc),
        "source": source,
        "source_id": str(source_id) if source_id else None,
        "company": company.strip(),
        "company_canonical": canon_company(company),
        "title": title.strip(),
        "location": loc.strip(),
        "remote": int(bool(re.search(r"remote|work from home|wfh", f"{loc} {title}", re.I))),
        "url": url,
        "apply_url": apply_url or url,
        "description": description[:20000],
        "salary_raw": salary_raw,
        "posted_at": posted_at,
    }


def get_json(url: str, **kw):
    r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True, **kw)
    r.raise_for_status()
    return r.json()
