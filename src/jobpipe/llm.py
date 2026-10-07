"""LLM client: rate limited, budget capped, PII redacting, JSON coercing.

Gemini is the default provider. Since 2026-09-27 a model name may also carry a
provider prefix -- "groq:qwen/qwen3.8-27b", "ollama:nemotron-3-ultra" -- and
`generate_json_chain()` walks an ordered list of them (TAILOR_CHAIN), one probe
per model. See "Providers" and "The chain" below. Every provider goes through
the same redaction, per-model budget and RPM window.

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
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx

from . import usage
from .config import DATA_DIR, env

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BUDGET_FILE = DATA_DIR / "gemini_budget.json"


class QuotaExhausted(RuntimeError):
    pass


class ProviderRejected(RuntimeError):
    """A 4xx other than 429: bad key, missing model, rejected parameter."""

    def __init__(self, message: str, status: int, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body

    @property
    def persistent(self) -> bool:
        """True when every later request to this model will fail the same way: a bad or
        revoked key (Gemini says so with HTTP 400 API_KEY_INVALID, not 401) or a missing model."""
        return (self.status in (401, 403, 404)
                or (self.status == 400 and ("API_KEY_INVALID" in self.body or "API key not valid" in self.body)))


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


# Google's free daily quota is per project PER MODEL, and the models do NOT all
# get the same number. Measured 2026-08-29 from the 429 body, which is the only
# place the real figure appears:
#
#   gemini-flash-latest  -> resolves to gemini-3.7-flash, quotaValue: 20
#   gemini-flash-lite-latest             90 calls in one run, no 429
#
# 7.26 split the COUNTER per model but left the CAP a single global number, so
# `cli status` cheerfully reported "481 left" on a model Google had already
# cut off at 20. That is bug 7.28's failure returning through a different
# door -- a counter that promises headroom the API has refused is worse than
# no counter, because it is trusted.
#
# Override per model with GEMINI_RPD_BUDGET_<model>, e.g.
#   GEMINI_RPD_BUDGET_gemini-flash-latest=20
# and GEMINI_RPD_BUDGET stays the default for anything unlisted.
DEFAULT_MODEL_CAPS = {
    "gemini-flash-latest": 20,
    # Pinned Gemini models in TAILOR_CHAIN (2026-09-27): the RPD column of AI Studio's
    # rate-limit page for this project (screenshot, 2026-09-27) -- Flash models 20/day,
    # Flash-Lite 500. gemini-flash-latest pointed at gemini-3.8-flash that day (the
    # modelVersion of a live reply), so the chain lists the alias, never both names:
    # two names for one quota would each get a counter and double the reported room.
    "gemini-3.8-flash": 20,
    "gemini-3.7-flash": 20,
    "gemini-3.6-flash": 20,
    "gemini-3.5-flash": 20,
    "gemini-3.1-flash-lite": 500,
    # Chain providers (2026-09-27). Guards, not measured 429s -- §6's rule is
    # about Gemini caps, which only a 429 body reveals. These are derived from
    # each provider's published limits and sized for tailoring (~5k tokens a call):
    #   Groq: 1,000 requests and 200,000 tokens per day per model (published,
    #   2026-09-27) -> ~40 tailor calls. The org's quota is shared with
    #   ContentPipe, which uses the same model, so a 429 can come early.
    "groq:qwen/qwen3.8-27b": 40,
    #   Ollama Cloud's free plan is monthly credits with no per-day figure
    #   published; 20/day keeps one run from spending the month.
    "ollama:nemotron-3-ultra": 20,
    "ollama:gemma4:31b": 20,
    #   Z.AI (2026-10-07): free Flash limits are not published; a guard like Groq's.
    "zai:glm-4.7-flash": 40,
    "zai:glm-4.5-flash": 40,
    "ollama:nemotron-3-super": 20,
    "ollama:gpt-oss:120b": 20,
    #   Same Groq account and limits as Qwen above.
    "groq:openai/gpt-oss-120b": 40,
}


def _env_name(model: str) -> str:
    """A model name as an env-var suffix: 'groq:qwen/qwen3.8-27b' -> 'groq_qwen_qwen3_8_27b'."""
    return re.sub(r"[^A-Za-z0-9_]", "_", model)


def _cap(model: str | None = None) -> int:
    default = int(env("GEMINI_RPD_BUDGET", "500"))
    if model is None:
        return default
    # The raw name first (the historical form, e.g. GEMINI_RPD_BUDGET_gemini-flash-latest),
    # then a shell-safe one, since a provider prefix brings ':' and '/' into the name.
    override = env(f"GEMINI_RPD_BUDGET_{model}", "") or env(f"GEMINI_RPD_BUDGET_{_env_name(model)}", "")
    if override:
        return int(override)
    return DEFAULT_MODEL_CAPS.get(model, default)


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
    cap = _cap(model)

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
    models = _today_models()
    if model is not None:
        return max(_cap(model) - (models.get(model) or {}).get("count", 0), 0)
    if not models:
        return _cap()
    # Each model has its own cap now, so the tightest is computed per model
    # rather than by finding the largest count against one shared number.
    return min(
        max(_cap(name) - (b or {}).get("count", 0), 0)
        for name, b in models.items()
    )


def budget_by_model() -> dict[str, int]:
    """Calls left today, per model that has been used. Empty before the first call."""
    return {m: max(_cap(m) - (b or {}).get("count", 0), 0)
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

# 90s was too tight and it cost a whole `prepare` run on 2026-08-29: every one
# of the 15 jobs failed, 5 budget slots burned per job, zero documents written.
# Measured on the real tailor prompt (10,687 chars, 46-fact menu):
#
#   gemini-flash-latest       HTTP 200 in  50.3s  (1,251 thinking tokens)
#   gemini-flash-lite-latest  HTTP 200 in   2.6s  (no thinking)
#
# A thinking model spends most of that time before the first byte arrives, so
# this is a READ timeout with nothing streaming to reset it. 50s nominal under
# a 90s ceiling leaves under 2x headroom, and Google's latency is not stable to
# 2x -- see the flakiness note in CLAUDE.md 6. 240s is ~5x the observed cost of
# the slowest call in the pipeline.
#
# Do NOT "fix" a timeout here by lowering max_output_tokens: 7.21 established
# that reasoning tokens are charged against that same ceiling, and cutting it
# truncates the JSON instead.
REQUEST_TIMEOUT = float(env("GEMINI_TIMEOUT", "240"))


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
# A model name with a known prefix ("groq:qwen/qwen3.8-27b") goes to that
# provider's OpenAI-compatible endpoint; anything else is a Gemini model, as it
# always was. The prefix is everything before the FIRST colon, because model ids
# themselves contain slashes and colons.
#
# THE RULE FOR THIS REGISTRY: résumé content goes to these providers, so a
# provider is listed only if its policy says it does not train on free-tier API
# prompts. Gemini's free tier does use prompts to improve Google's products; it
# was here first and is covered by redact() (module docstring). Checked and left
# OUT on purpose (2026-09-27): Mistral's free tier (trains on prompts),
# Requesty's free Nemotron models ("Training Permitted Models"), and OpenRouter
# `:free` models (training depends on the upstream provider). Do not add a
# provider without reading its data policy and recording the source here.
_BUILTIN_PROVIDERS: dict[str, dict] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_envs": ["GROQ_API_KEY"],
        "max_tokens_param": "max_completion_tokens",
        # "Groq is not permitted to use Inputs or Outputs for training or fine-tuning ... unless
        # explicitly granted permission" -- console.groq.com/docs/legal/services-agreement §4.2,
        # read 2026-09-27. May log up to 30 days for abuse/reliability.
        "policy": "no training (Services Agreement §4.2, 2026-09-27)",
    },
    "ollama": {
        "base_url": "https://ollama.com/v1",
        # OLAMA_API_KEY: the misspelling both .env files were first saved with (ContentPipe reads it too).
        "key_envs": ["OLLAMA_API_KEY", "OLAMA_API_KEY"],
        "max_tokens_param": "max_tokens",
        # "we process your prompts and responses transiently to provide the service and never
        # train on it" -- ollama.com/privacy, read 2026-09-27.
        "policy": "no training, transient processing (privacy page, 2026-09-27)",
    },
}


# The shared catalog (LLM_CATALOG, the providers.json in the LLM-Catalog repo,
# also read by ContentPipe) records every provider once, with its training
# policy and where and when that was read. Only its `trainsOnPrompts: false`
# entries come in here, and they replace the built-in entry of the same id; an
# entry marked anything else (true, "depends", "unknown") removes the provider
# even if it is built in, because the catalog is the newer record. Without
# LLM_CATALOG (Docker, CI, a fresh clone) the built-in registry above is used.
# A configured catalog that is unreadable or malformed raises: a résumé must
# never reach a provider because a policy record failed to load.
_LOCAL_URL = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)")


def providers_from_catalog(path: str | os.PathLike | None,
                           builtin: dict[str, dict] = _BUILTIN_PROVIDERS) -> dict[str, dict]:
    """The provider registry: `builtin`, overlaid by the catalog at `path` (if any)."""
    out = dict(builtin)
    if not path or not str(path).strip():
        return out
    where = f"LLM_CATALOG {path}"
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise RuntimeError(f"{where}: {e}") from e
    providers = doc.get("providers") if isinstance(doc, dict) and doc.get("version") == 1 else None
    if not isinstance(providers, list) or not providers:
        raise RuntimeError(f'{where}: expected {{"version": 1, "providers": [...]}}')
    for i, p in enumerate(providers):
        pid = p.get("id") if isinstance(p, dict) else None
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z][a-z0-9]*", pid) or pid == "gemini":
            raise RuntimeError(f"{where}: providers[{i}] has a bad id {pid!r}")
        if p.get("trainsOnPrompts") is not False:
            out.pop(pid, None)
            continue
        url, keys, policy = p.get("baseUrl"), p.get("keyEnv"), p.get("policy")
        if not isinstance(url, str) or not (url.startswith("https://") or _LOCAL_URL.match(url)) or url.endswith("/"):
            raise RuntimeError(f"{where}: {pid}: baseUrl must be https:// (http only for localhost), no trailing slash")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", k) for k in keys):
            raise RuntimeError(f"{where}: {pid}: keyEnv must be a non-empty list of env var names")
        if p.get("maxTokensParam") not in ("max_tokens", "max_completion_tokens"):
            raise RuntimeError(f"{where}: {pid}: bad maxTokensParam")
        if not isinstance(policy, str) or "no training" not in policy:
            raise RuntimeError(f'{where}: {pid}: trainsOnPrompts is false but its policy does not say "no training" with a source')
        out[pid] = {"base_url": url, "key_envs": keys, "max_tokens_param": p["maxTokensParam"], "policy": policy}
    return out


PROVIDERS: dict[str, dict] = providers_from_catalog(env("LLM_CATALOG"))


def split_model(name: str) -> tuple[str, str]:
    """('groq', 'qwen/qwen3.8-27b') for 'groq:qwen/qwen3.8-27b'; ('gemini', name) otherwise."""
    head, sep, rest = name.partition(":")
    if sep and rest and head.lower() in PROVIDERS:
        return head.lower(), rest
    if sep and rest and head.lower() == "gemini":
        return "gemini", rest
    if sep:
        # Gemini model names never contain a colon, so this is a provider we do not
        # know -- most likely one left out on purpose (it trains on prompts). Say so,
        # rather than sending "mistral:..." to Gemini as a model name.
        raise ValueError(f"{name!r}: unknown provider {head!r}. Allowed: gemini, "
                         f"{', '.join(PROVIDERS)} (only providers that do not train on prompts; see llm.PROVIDERS).")
    return "gemini", name


def refusal(model: str) -> str | None:
    """Why this model name is refused (an unknown or training provider), or None."""
    try:
        split_model(model)
        return None
    except ValueError as e:
        return str(e)


def has_key(model: str) -> bool:
    """Whether the provider behind this model name is configured at all. A refused
    name has no provider, so False: callers that size or list the chain must not
    crash on one bad TAILOR_CHAIN entry (the chain itself logs the refusal)."""
    if refusal(model):
        return False
    provider, _ = split_model(model)
    return bool(_provider_key(provider))


def _provider_key(provider: str) -> str:
    names = ["GEMINI_API_KEY"] if provider == "gemini" else PROVIDERS[provider]["key_envs"]
    return next((v for v in ((env(n) or "").strip() for n in names) if v), "")


# Groq blocks some default HTTP-library User-Agents with an empty 403 (seen
# from ContentPipe with urllib); an honest, specific one passes.
_USER_AGENT = "JobPipe/1.0 (personal job-search pipeline)"


def _generate_openai(prompt: str, provider: str, model_id: str, *, json_out: bool,
                     temperature: float, max_output_tokens: int) -> httpx.Response:
    spec = PROVIDERS[provider]
    key = _provider_key(provider)
    if not key:
        raise RuntimeError(f"{' / '.join(spec['key_envs'])} is not set; {provider} models are unavailable.")
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        spec["max_tokens_param"]: max_output_tokens,
    }
    if json_out:
        payload["response_format"] = {"type": "json_object"}
    return httpx.post(f"{spec['base_url']}/chat/completions", json=payload, timeout=REQUEST_TIMEOUT,
                      headers={"Authorization": f"Bearer {key}", "User-Agent": _USER_AGENT})


def _openai_text(data: dict, model: str, max_output_tokens: int) -> str:
    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "")
    # Open reasoning models can wrap their thinking in <think> tags ahead of the answer.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if choice.get("finish_reason") == "length":
        raise RuntimeError(f"{model} hit the {max_output_tokens}-token output cap and the reply is truncated.")
    if not text:
        raise RuntimeError(f"{model} returned no content")
    return text


def _generate_with_retries(send, parse, *, model: str, max_retries: int, provider: str = "gemini") -> str:
    """The one retry loop, for Gemini and every chain provider.

    A slot is reserved per ATTEMPT (a retry is a real request that the provider
    counts, so the budget counts it too -- a bad retry loop can eat the day, and
    that is the intended, visible behaviour). 429 backs off with jitter, 5xx and
    network errors back off; no sleep after the last attempt. The final error
    names every attempt's actual cause: the old code ended with a flat "rate
    limited" even when the cause was a timeout or a 503, and sent a whole
    session chasing a quota problem that did not exist (7.22, 7.32).
    """
    delay = 2.0
    failures: list[str] = []
    # Every request the provider RECEIVES goes in the usage log (usage.py), for the
    # dashboard's "models used, last 48 h". A spent budget sends nothing, so logs nothing.
    name = model.split(":", 1)[1] if provider != "gemini" and ":" in model else model
    for attempt in range(max_retries):
        _reserve_slot(model)
        last = attempt == max_retries - 1
        t0 = time.time()
        took = lambda: int((time.time() - t0) * 1000)  # noqa: E731
        try:
            r = send()
        except httpx.RequestError as e:
            failures.append(type(e).__name__)
            usage.record(provider, name, "error", ms=took(), detail=type(e).__name__)
            if last:
                raise
            time.sleep(delay + random.uniform(0, 1))
            delay *= 2
            continue
        if r.status_code == 429:
            # Exponential backoff WITH jitter. Immediate retry makes it worse.
            failures.append("429")
            usage.record(provider, name, "quota", ms=took(), detail="HTTP 429")
            if not last:
                time.sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, 60)
            continue
        if r.status_code >= 500:
            failures.append(str(r.status_code))
            usage.record(provider, name, "overloaded", ms=took(), detail=f"HTTP {r.status_code}")
            if not last:
                time.sleep(delay + random.uniform(0, 1))
                delay *= 2
            continue
        if r.status_code >= 400:
            # A bad key, a missing model, a rejected parameter: retrying cannot help.
            # The body only, never the request URL.
            usage.record(provider, name, "error", ms=took(), detail=f"HTTP {r.status_code}")
            raise ProviderRejected(f"{model} answered HTTP {r.status_code}: {r.text[:200]}", r.status_code, r.text)
        try:
            text = parse(r)
        except Exception as e:
            usage.record(provider, name, "invalid_output", ms=took(), detail=str(e)[:120])
            raise
        usage.record(provider, name, "ok", ms=took())
        return text
    seen = ", ".join(failures) or "no attempts recorded"
    raise QuotaExhausted(
        f"Exhausted {max_retries} retries against {model} [{seen}]. "
        f"429 = quota or rate limit; 5xx = provider-side; ReadTimeout = the call "
        f"took longer than GEMINI_TIMEOUT ({REQUEST_TIMEOUT:.0f}s).")


def _gemini_text(data: dict, max_output_tokens: int) -> str:
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


def generate(prompt: str, *, model: str, json_out: bool = True,
             temperature: float = 0.2, max_retries: int = 5,
             max_output_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    provider, model_id = split_model(model)
    if provider != "gemini":
        return _generate_with_retries(
            lambda: _generate_openai(redact(prompt), provider, model_id, json_out=json_out,
                                     temperature=temperature, max_output_tokens=max_output_tokens),
            lambda r: _openai_text(r.json(), model, max_output_tokens),
            model=f"{provider}:{model_id}", max_retries=max_retries, provider=provider)

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
    url = ENDPOINT.format(model=model_id)
    # Key in a header, not ?key=: an httpx error's message carries the full
    # request URL, and `cli rescreen` prints the start of an error message.
    return _generate_with_retries(
        lambda: httpx.post(url, headers={"x-goog-api-key": key}, json=payload, timeout=REQUEST_TIMEOUT),
        lambda r: _gemini_text(r.json(), max_output_tokens),
        model=model_id, max_retries=max_retries)


def generate_json(prompt: str, *, model: str, temperature: float = 0.2,
                  max_output_tokens: int = MAX_OUTPUT_TOKENS, max_retries: int = 5) -> dict:
    raw = generate(prompt, model=model, json_out=True, temperature=temperature,
                   max_output_tokens=max_output_tokens, max_retries=max_retries)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON: {e}\n---\n{cleaned[:500]}")


# --------------------------------------------------------------------------
# The chain
# --------------------------------------------------------------------------
# `generate_json_chain` tries an ordered list of models and returns the first
# usable answer AND the model that gave it, so provenance records the model
# that actually wrote the document, not the one that was asked first.
#
# One probe per model, then the next one. §6 has said "one probe, then fall
# back" since 2026-09-01, yet generate() retried a 503 five times and charged
# each retry to flash-latest's 20/day: [503, 503, 503, 503, 503] in the logs of
# 2026-09-25, -26 and -27. A model that fails is benched for the rest of the
# run, so the next job does not spend another probe on it.
#
# EXCEPT the last model standing. It has nothing to fall back to, so it keeps
# the full retry loop, as flash-lite had when it was the fallback stage: one
# routine 503 on the last resort must not stop the day's prepare with hundreds
# of calls unspent. A single pinned model is the last model standing too.
BENCH_SECONDS_SICK = 900     # 5xx, timeout, bad key, missing model: down for now
BENCH_SECONDS_BUSY = 90      # 429: a per-minute limit, usually
LAST_RESORT_RETRIES = 5
_benched: dict[str, float] = {}


def reset_bench() -> None:
    """Test hook: the bench is process state."""
    _benched.clear()


def _canonical(model: str) -> str:
    """The budget-counter key: 'provider:model' with the provider lowercased, and Gemini
    names without a 'gemini:' prefix, as they always were. A refused name is returned
    unchanged (it is never called)."""
    if refusal(model):
        return model
    provider, model_id = split_model(model)
    return model_id if provider == "gemini" else f"{provider}:{model_id}"


def generate_json_chain(prompt: str, *, models: list[str], temperature: float = 0.2,
                        max_output_tokens: int = MAX_OUTPUT_TOKENS) -> tuple[dict, str]:
    """(parsed JSON, model that answered). Raises QuotaExhausted when every model
    is spent, benched, unconfigured or refused -- the caller's cue to stop for the
    day -- and RuntimeError when models answered but none usably (bad JSON,
    truncated), which is about this one prompt, not the day."""
    tried: list[str] = []
    candidates: list[str] = []
    for raw in models:
        why = refusal(raw)
        if why:
            tried.append(f"{raw}: refused, not an allowed provider")
            continue
        m = _canonical(raw)
        if m in candidates:
            continue
        if not has_key(m):
            tried.append(f"{m}: no key")
        elif _benched.get(m, 0) > time.time():
            tried.append(f"{m}: benched")
        elif budget_remaining(m) < 1:
            tried.append(f"{m}: budget spent")
        else:
            candidates.append(m)

    only_quota = True
    for i, m in enumerate(candidates):
        retries = LAST_RESORT_RETRIES if i == len(candidates) - 1 else 1
        try:
            return generate_json(prompt, model=m, temperature=temperature,
                                 max_output_tokens=max_output_tokens, max_retries=retries), m
        except QuotaExhausted as e:
            # "[503]" after one probe, "[503, 503, 429, ...]" after the last resort's retries.
            # A spent daily budget (from _reserve_slot) has no brackets.
            found = re.search(r"\[([^\]]+)\]", str(e))
            causes = found.group(1).split(", ") if found else ["budget spent"]
            busy = all(c == "429" for c in causes)
            _benched[m] = time.time() + (BENCH_SECONDS_BUSY if busy else BENCH_SECONDS_SICK)
            tried.append(f"{m}: {', '.join(causes)}")
        except httpx.RequestError as e:
            _benched[m] = time.time() + BENCH_SECONDS_SICK
            tried.append(f"{m}: {type(e).__name__}")
        except ProviderRejected as e:
            # A key or missing-model failure repeats on every job: bench, move on.
            # Any other 4xx is about this request, like bad JSON below.
            if e.persistent:
                _benched[m] = time.time() + BENCH_SECONDS_SICK
            else:
                only_quota = False
            tried.append(f"{m}: HTTP {e.status}{' (bad key or missing model)' if e.persistent else ''}")
        except (RuntimeError, ValueError) as e:
            # Answered, but not usably for THIS prompt (bad JSON, truncated):
            # the next model may do better, and the next prompt may be fine here.
            only_quota = False
            tried.append(f"{m}: {str(e)[:120]}")
    summary = "; ".join(tried) or "no models configured"
    if only_quota:
        raise QuotaExhausted(f"No model in the chain could answer: {summary}")
    raise RuntimeError(f"No model in the chain gave a usable answer: {summary}")
