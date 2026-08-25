"""Preflight check. Run before anything else:  python scripts/doctor.py

Answers, empirically:
  - is my Gemini key valid
  - which models can it actually call
  - am I on the free tier or a paid (Tier 1+) project
  - what should MODEL_SCORE / MODEL_TAILOR / GEMINI_RPM be set to
  - are Gmail, Telegram and Adzuna wired up
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://generativelanguage.googleapis.com/v1beta"
OK, WARN, BAD = "  [ok]  ", "  [warn]", "  [FAIL]"


def line(status, msg):
    print(f"{status} {msg}")


def check_key() -> str | None:
    print("\n== Gemini key ==")
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        line(BAD, "GEMINI_API_KEY is empty. Copy .env.example to .env and paste your key.")
        return None
    if not key.startswith("AIza"):
        line(WARN, "Key doesn't look like an AI Studio key (expected it to start with AIza).")
    try:
        r = httpx.get(f"{BASE}/models", params={"key": key}, timeout=30)
    except httpx.RequestError as e:
        line(BAD, f"Cannot reach Google: {e}")
        return None
    if r.status_code == 400:
        line(BAD, "Key rejected. Regenerate it at aistudio.google.com.")
        return None
    if r.status_code != 200:
        line(BAD, f"HTTP {r.status_code}: {r.text[:200]}")
        return None
    line(OK, "Key is valid.")
    return key


# Models that are not text generators, regardless of what they are named.
# nano-banana = image, lyria = music. Both advertise generateContent.
NON_TEXT = ("embedding", "aqa", "imagen", "veo", "tts", "nano-banana", "lyria",
            "-image", "-audio", "native-audio", "-live", "-dialog", "robotics")


def rank(name: str) -> tuple:
    """Prefer stable over preview, then -latest aliases, then higher version.

    Reverse-alphabetical sorting is what put an image model at the top of the
    Pro list on the first pass. Do not go back to it.
    """
    import re as _re
    preview = ("preview" in name) or ("-exp" in name) or ("experimental" in name)
    latest = name.endswith("-latest")
    m = _re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    ver = float(m.group(1)) if m else 0.0
    return (not preview, latest, ver)


def best(names: list[str], family: str, exclude: tuple = ()) -> str | None:
    pool = [n for n in names
            if family in n
            and not any(x in n for x in NON_TEXT)
            and not any(x in n for x in exclude)]
    return sorted(pool, key=rank, reverse=True)[0] if pool else None


def quota_id(resp) -> str:
    """Free-tier 429s name their quota. This is the definitive tier signal."""
    try:
        for d in resp.json().get("error", {}).get("details", []):
            for v in d.get("violations", []):
                q = v.get("quotaId", "") or v.get("quotaMetric", "")
                if q:
                    return q
    except Exception:
        pass
    return ""


def list_models(key: str) -> list[str]:
    print("\n== Models your key can call ==")
    r = httpx.get(f"{BASE}/models", params={"key": key}, timeout=30)
    names = []
    for m in r.json().get("models", []):
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        names.append(m["name"].removeprefix("models/"))

    text = [n for n in names if not any(x in n for x in NON_TEXT)]
    dropped = len(names) - len(text)

    for family in ("flash", "pro"):
        pool = sorted([n for n in text if family in n], key=rank, reverse=True)
        print(f"  {family.title()} (best first):")
        for n in pool[:5]:
            tag = " [preview]" if "preview" in n else ""
            print(f"    - {n}{tag}")
    if dropped:
        line(OK, f"Ignored {dropped} non-text model(s) (image/audio/embedding).")
    return text


def probe_tier(key: str, models: list[str]) -> str:
    """Test a STABLE Pro text model, and read the quota id on any 429."""
    print("\n== Tier probe ==")
    target = best(models, "pro", exclude=("flash",))
    if not target:
        line(WARN, "FREE TIER (no Pro text model available).")
        return "free"

    print(f"  probing {target}")
    body = {"contents": [{"parts": [{"text": "Reply with one word: ok"}]}],
            "generationConfig": {"maxOutputTokens": 16}}

    for attempt in range(3):
        try:
            r = httpx.post(f"{BASE}/models/{target}:generateContent",
                           params={"key": key}, json=body, timeout=60)
        except httpx.RequestError as e:
            line(WARN, f"Probe failed ({e}). Assuming free tier.")
            return "free"

        if r.status_code == 200:
            line(OK, f"PAID TIER - {target} answered. Billing is on for this project.")
            return "paid"

        if r.status_code == 429:
            q = quota_id(r)
            if "free" in q.lower() or "FreeTier" in q:
                line(WARN, f"FREE TIER - quota hit: {q}")
                return "free"
            if attempt < 2:
                print(f"  429 (quota id: {q or 'unnamed'}) - retrying in 20s...")
                time.sleep(20)
                continue
            line(WARN, f"Repeated 429 on {target}. Quota id: {q or 'unnamed'}.")
            line(WARN, "Inconclusive - could be a shared preview quota. Assuming free.")
            return "free"

        if r.status_code == 404:
            line(WARN, f"{target} not available to this key. Assuming free tier.")
            return "free"

        line(WARN, f"HTTP {r.status_code}: {r.text[:160]}")
        return "free"
    return "free"


def measure_rpm(key: str, model: str) -> None:
    print(f"\n== Rate check ({model}) ==")
    body = {"contents": [{"parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 8}}
    ok = 0
    start = time.time()
    for i in range(6):
        r = httpx.post(f"{BASE}/models/{model}:generateContent",
                       params={"key": key}, json=body, timeout=45)
        if r.status_code == 200:
            ok += 1
        elif r.status_code == 429:
            q = quota_id(r)
            line(WARN, f"429 after {ok} call(s). Quota: {q or 'unnamed'}")
            if ok == 0:
                line(WARN, "Zero succeeded - the model is gated, or today's quota is spent.")
            break
        else:
            line(WARN, f"HTTP {r.status_code} on call {i+1}: {r.text[:120]}")
            break
        time.sleep(0.4)
    if ok:
        line(OK, f"{ok}/6 rapid calls succeeded in {time.time()-start:.1f}s")


def recommend(tier: str, models: list[str]) -> None:
    print("\n== Put this in your .env ==")
    lite = best(models, "flash-lite") or best(models, "lite")
    flash = best(models, "flash", exclude=("lite",))
    pro = best(models, "pro", exclude=("flash",))

    score = lite or flash
    tailor = (pro if tier == "paid" else flash) or flash
    rpm, rpd = (60, 5000) if tier == "paid" else (12, 1200)

    print(f"""
MODEL_SCORE={score}
MODEL_TAILOR={tailor}
GEMINI_RPM={rpm}
GEMINI_RPD_BUDGET={rpd}
""")
    line(OK, "These are stable aliases - they track Google's current model without breaking.")
    if tier == "paid":
        line(OK, "Paid tier: prompts are NOT used for training. Redaction stays on regardless.")
    else:
        line(WARN, "Free tier: prompts may train Google's models. Fill in PII_DENY_TERMS.")
    print("  Avoid any model tagged [preview] - tight, unpredictable quotas.")


def check_config() -> None:
    print("\n== Config files ==")
    import yaml
    root = Path(__file__).resolve().parents[1]

    for name in ("profile.yaml", "facts.yaml", "companies.yaml"):
        p = root / "config" / name
        line(OK if p.exists() else BAD, f"config/{name}")

    facts_path = root / "config" / "facts.yaml"
    if facts_path.exists():
        cfg = yaml.safe_load(facts_path.read_text()) or {}
        total = verified = 0
        for group in (cfg.get("roles") or []) + (cfg.get("projects") or []):
            for f in group.get("facts") or []:
                total += 1
                verified += bool(f.get("verified"))
        if verified == 0:
            line(BAD, f"0 of {total} facts verified. Nothing can be tailored yet.")
            print("         Open config/facts.yaml, correct each line to what you")
            print("         actually did, then set verified: true.")
        elif verified < 6:
            line(WARN, f"{verified}/{total} facts verified. Tailoring wants 6-9 to choose from.")
        else:
            line(OK, f"{verified}/{total} facts verified.")


def check_optional() -> None:
    print("\n== Optional integrations ==")
    root = Path(__file__).resolve().parents[1]

    tg = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    line(OK if tg else WARN, "Telegram" if tg else "Telegram not set — queue prints to stdout")

    adz = bool(os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY"))
    line(OK if adz else WARN, "Adzuna" if adz else "Adzuna not set — ATS sources only")

    creds = root / (os.getenv("GMAIL_CREDENTIALS_JSON", "data/gmail_credentials.json").lstrip("./"))
    token = root / (os.getenv("GMAIL_TOKEN_JSON", "data/gmail_token.json").lstrip("./"))
    if token.exists():
        line(OK, "Gmail authorised")
    elif creds.exists():
        line(WARN, "Gmail credentials present, not yet authorised — first run opens a browser")
    else:
        line(WARN, "Gmail not set — LinkedIn/Naukri alerts will not be ingested")

    deny = os.getenv("PII_DENY_TERMS", "")
    line(OK if deny else WARN,
         f"PII_DENY_TERMS: {deny}" if deny else "PII_DENY_TERMS empty — add your employer name")


if __name__ == "__main__":
    print("jobpipe doctor")
    key = check_key()
    if not key:
        sys.exit(1)
    models = list_models(key)
    tier = probe_tier(key, models)
    f = best(models, "flash", exclude=("lite",))
    if f:
        measure_rpm(key, f)
    recommend(tier, models)
    check_config()
    check_optional()
    print("\nIf facts.yaml shows 0 verified, that is the next thing to fix.\n")