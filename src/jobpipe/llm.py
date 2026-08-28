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

import fcntl
import json
import os
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx

from .config import DATA_DIR, env

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BUDGET_FILE = DATA_DIR / "gemini_budget.json"


class QuotaExhausted(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Whose midnight?
# --------------------------------------------------------------------------
# Google's free-tier quota rolls at midnight America/Los_Angeles. In IST that
# is 12:30 -- the middle of this user's working day, not the middle of the
# night. A counter rolling on the LOCAL date therefore shadows a different
# window than the quota it exists to track, for half of every day.
#
# Measured 2026-08-28: ~946 flash-lite calls got through a documented 500/day
# cap without the counter noticing, because the session straddled 12:30 IST and
# was really two quota days. The counter was not wrong about its own arithmetic
# -- it was counting the wrong day.
PACIFIC = "America/Los_Angeles"


def _quota_day() -> str:
    """Today's date in Google's quota timezone, as an ISO string."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(PACIFIC)).date().isoformat()
    except Exception:
        # python:*-slim carries no /usr/share/zoneinfo. The `tzdata` package in
        # requirements.txt covers that, and zoneinfo falls back to it
        # automatically -- but if even that is absent, degrade rather than
        # crash the whole client over a date.
        #
        # -8 (PST) deliberately, never -7. During DST the real boundary is -7,
        # so a fixed -8 rolls our day an hour LATE: the counter keeps counting
        # after Google has reset, which under-allows. The opposite error would
        # reset us early and spend into a 429.
        return datetime.now(timezone(timedelta(hours=-8))).date().isoformat()


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
# Rate limiting + daily budget -- CROSS-PROCESS
#
# Both used to be module state, which is fine until the compose scheduler runs
# `score` while you run `prepare` by hand. Then there are two token buckets,
# each politely staying under 12 RPM, and Google sees 24. The budget counter
# had the same problem in worse form: an unlocked read-modify-write on a JSON
# file, so concurrent increments were simply lost.
#
# One file, one flock, holding both the day's count and the recent request
# timestamps. Every process coordinates through it. Timestamps are wall-clock,
# not monotonic -- monotonic clocks are not comparable across processes.
# --------------------------------------------------------------------------
STATE_FILE = BUDGET_FILE          # kept under the old name; same file on disk


@contextmanager
def _locked_state():
    """Exclusive access to the shared counter file. Always writes back.

    The write must be flushed AND fsynced before the lock is released. Python
    file objects buffer, and a buffer that flushes on close() flushes after the
    unlock -- which lets the next process read stale state and lose the
    increment. That defeats the entire point of taking the lock.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = b""
        while chunk := os.read(fd, 65536):
            raw += chunk
        try:
            state = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            state = {}
        today = _quota_day()
        # A file from before the per-model split has {"count", "stamps"} at the
        # top level and no way to say which model spent them. It is discarded
        # rather than guessed at: the counter is a courtesy guard, Google
        # enforces the real cap, and mis-attributing yesterday's spend would
        # lock out a model whose quota is untouched. Self-heals at midnight.
        if state.get("date") != today or "models" not in state:
            state = {"date": today, "models": {}}
        state.setdefault("models", {})

        yield state

        payload = json.dumps(state).encode()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _bucket(state: dict, model: str) -> dict:
    """This model's slice of today's state. Created on first use."""
    b = state["models"].setdefault(model, {})
    b.setdefault("count", 0)
    b.setdefault("stamps", [])
    return b


def _cap() -> int:
    # 500, measured -- see CLAUDE.md 6. The old default of 1200 was fiction and
    # let the counter report headroom that Google had already refused.
    return int(env("GEMINI_RPD_BUDGET", "500"))


def _reserve_slot(model: str) -> None:
    """Claim one request against this MODEL's RPM window and daily budget.

    Google's free-tier quotas are per project PER MODEL -- the 429 names them
    `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, measured at 500/day on
    2026-08-28. One shared counter therefore let a long `score` run lock out
    `prepare`, whose model had spent nothing. Each model now gets its own count
    and its own RPM window.

    The RPM window is split the same way. Only the DAILY quota was observed
    directly; Google names its per-minute quota with the same PerModel suffix,
    so this follows it. If that turns out to be wrong the failure is visible
    and safe -- 429s with backoff, not silent overspend.

    Blocks until a slot is free. The lock is released while sleeping so other
    processes are not held up behind us.
    """
    rpm = int(env("GEMINI_RPM", "10"))
    cap = _cap()

    while True:
        with _locked_state() as state:
            b = _bucket(state, model)
            if b["count"] >= cap:
                raise QuotaExhausted(
                    f"Daily budget of {cap} Gemini calls for {model} reached. "
                    "Resets at midnight Pacific. Other models are unaffected."
                )
            wall = time.time()
            b["stamps"] = [s for s in b["stamps"] if wall - s < 60]
            if len(b["stamps"]) < rpm:
                b["stamps"].append(wall)
                b["count"] += 1
                return
            sleep_for = 60 - (wall - min(b["stamps"])) + 0.5
        time.sleep(max(sleep_for, 0.1))


def _today_models() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text() or "{}")
    except json.JSONDecodeError:
        return {}
    if state.get("date") != _quota_day():
        return {}
    return state.get("models") or {}


def budget_remaining(model: str | None = None) -> int:
    """Calls left today. For one model, or the tightest across those used.

    No argument answers "how many more calls am I sure of", which is what a
    status line wants -- so it reports the most-spent model, not a total.
    """
    cap, models = _cap(), _today_models()
    if model is not None:
        return max(cap - (models.get(model) or {}).get("count", 0), 0)
    if not models:
        return cap
    return max(cap - max((b or {}).get("count", 0) for b in models.values()), 0)


def budget_by_model() -> dict[str, int]:
    """Calls left today, per model that has been used. Empty before the first call."""
    cap = _cap()
    return {m: max(cap - (b or {}).get("count", 0), 0)
            for m, b in sorted(_today_models().items())}


# --------------------------------------------------------------------------
# Call
# --------------------------------------------------------------------------
# On a thinking model, reasoning tokens are charged against maxOutputTokens
# alongside the answer. Measured on the tailor prompt with gemini-3.6-flash:
# 1,646 thinking + 398 answer against a 2,048 ceiling -> finishReason
# MAX_TOKENS, JSON cut off mid-string, ValueError. The same call at 8,192 spent
# 2,538 thinking + 658 answer and finished clean. 2048 was sized for a
# non-thinking model and is no longer a safe ceiling for anything structured.
# (thinkingConfig.thinkingBudget=0 is not accepted by this model -- HTTP 400.)
MAX_OUTPUT_TOKENS = 8192


def generate(prompt: str, *, model: str, json_out: bool = True,
             temperature: float = 0.2, max_retries: int = 5,
             max_output_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    key = env("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set. Copy .env.example to .env.")

    payload = {
        "contents": [{"parts": [{"text": redact(prompt)}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }
    if json_out:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    url = ENDPOINT.format(model=model)
    delay = 2.0

    for attempt in range(max_retries):
        # Reserved per ATTEMPT, not per call: a retry is a real request that
        # Google counts, so the budget must count it too. A bad retry loop can
        # therefore eat the day -- that is the intended, visible behaviour.
        _reserve_slot(model)
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
        cand = (data.get("candidates") or [{}])[0]
        # Thinking models can split the answer across parts. Joining them is
        # correct for a single-part reply too.
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text") or "" for p in parts)
        if not text:
            reason = data.get("promptFeedback", {}).get("blockReason", "unknown")
            raise RuntimeError(f"Gemini returned no content (reason: {reason})")
        if cand.get("finishReason") == "MAX_TOKENS":
            um = data.get("usageMetadata", {})
            raise RuntimeError(
                "Gemini hit maxOutputTokens and the reply is truncated "
                f"(thinking={um.get('thoughtsTokenCount')}, "
                f"answer={um.get('candidatesTokenCount')}, "
                f"ceiling={max_output_tokens}). Raise max_output_tokens.")
        return text

    raise QuotaExhausted("Exhausted retries against Gemini (rate limited).")


def generate_json(prompt: str, *, model: str, temperature: float = 0.2,
                  max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
    raw = generate(prompt, model=model, json_out=True, temperature=temperature,
                   max_output_tokens=max_output_tokens)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON: {e}\n---\n{cleaned[:500]}")
