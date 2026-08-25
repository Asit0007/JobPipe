"""Gemini client: rate limited, budget capped, PII redacting, JSON coercing.

Design notes
------------
Free tier is ~15 RPM / 1500 RPD on Flash. Two consequences baked in here:

1. Requests are SEQUENTIAL through a token bucket. No asyncio.gather -- parallel
   fan-out is the single fastest way to eat a 429 on this tier.
2. A daily budget counter persists to disk so a runaway loop cannot burn the
   whole quota at 3am and leave you with nothing at 9am.

Google may use free-tier prompts and responses to improve their models, so
redact() strips identifying fields before anything leaves this process. The
pipeline is built so the model never needs them: it works on fact IDs and job
descriptions, and PII is reattached locally at render time.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

from .config import DATA_DIR, env

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BUDGET_FILE = DATA_DIR / "gemini_budget.json"


class QuotaExhausted(RuntimeError):
    pass


# --------------------------------------------------------------------------
# PII redaction. Belt and braces: patterns AND an explicit deny list.
# --------------------------------------------------------------------------
_PATTERNS = [
    (re.compile(r"[\w\.\-\+]+@[\w\-]+\.[\w\.\-]+"), "[EMAIL]"),
    (re.compile(r"(?:\+91[\-\s]?)?\b[6-9]\d{9}\b"), "[PHONE]"),
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[ID]"),
    (re.compile(r"\bhttps?://(?:www\.)?linkedin\.com/in/[\w\-]+"), "[PROFILE]"),
]

DENY_TERMS = [t for t in (env("PII_DENY_TERMS", "") or "").split(",") if t.strip()]


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    for term in DENY_TERMS:
        out = re.sub(re.escape(term.strip()), "[REDACTED]", out, flags=re.I)
    return out


# --------------------------------------------------------------------------
# Rate limiting + daily budget
# --------------------------------------------------------------------------
@dataclass
class _Bucket:
    rpm: int
    _stamps: list[float]

    def wait(self) -> None:
        now = time.monotonic()
        self._stamps[:] = [s for s in self._stamps if now - s < 60]
        if len(self._stamps) >= self.rpm:
            sleep_for = 60 - (now - self._stamps[0]) + 0.5
            time.sleep(max(sleep_for, 0))
            self.wait()
            return
        self._stamps.append(time.monotonic())


_bucket = _Bucket(rpm=int(env("GEMINI_RPM", "12")), _stamps=[])


def _budget_check_and_increment() -> None:
    cap = int(env("GEMINI_RPD_BUDGET", "1200"))
    today = date.today().isoformat()
    state = {"date": today, "count": 0}
    if BUDGET_FILE.exists():
        try:
            loaded = json.loads(BUDGET_FILE.read_text())
            if loaded.get("date") == today:
                state = loaded
        except json.JSONDecodeError:
            pass
    if state["count"] >= cap:
        raise QuotaExhausted(
            f"Daily budget of {cap} Gemini calls reached. Resets at midnight Pacific."
        )
    state["count"] += 1
    BUDGET_FILE.write_text(json.dumps(state))


def budget_remaining() -> int:
    cap = int(env("GEMINI_RPD_BUDGET", "1200"))
    if not BUDGET_FILE.exists():
        return cap
    try:
        state = json.loads(BUDGET_FILE.read_text())
    except json.JSONDecodeError:
        return cap
    if state.get("date") != date.today().isoformat():
        return cap
    return max(cap - state.get("count", 0), 0)


# --------------------------------------------------------------------------
# Call
# --------------------------------------------------------------------------
def generate(prompt: str, *, model: str, json_out: bool = True,
             temperature: float = 0.2, max_retries: int = 5) -> str:
    key = env("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set. Copy .env.example to .env.")

    payload = {
        "contents": [{"parts": [{"text": redact(prompt)}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 2048,
        },
    }
    if json_out:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    url = ENDPOINT.format(model=model)
    delay = 2.0

    for attempt in range(max_retries):
        _budget_check_and_increment()
        _bucket.wait()
        try:
            r = httpx.post(url, params={"key": key}, json=payload, timeout=90)
        except httpx.RequestError as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(delay + random.uniform(0, 1))
            delay *= 2
            continue

        if r.status_code == 429:
            # Exponential backoff WITH jitter. Immediate retry makes it worse.
            sleep_for = delay + random.uniform(0, delay / 2)
            time.sleep(sleep_for)
            delay = min(delay * 2, 60)
            continue

        if r.status_code >= 500:
            time.sleep(delay + random.uniform(0, 1))
            delay *= 2
            continue

        r.raise_for_status()
        data = r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            reason = data.get("promptFeedback", {}).get("blockReason", "unknown")
            raise RuntimeError(f"Gemini returned no content (reason: {reason})")

    raise QuotaExhausted("Exhausted retries against Gemini (rate limited).")


def generate_json(prompt: str, *, model: str, temperature: float = 0.2) -> dict:
    raw = generate(prompt, model=model, json_out=True, temperature=temperature)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON: {e}\n---\n{cleaned[:500]}")
