"""The provider chain for tailoring and screening (2026-09-27).

Two properties matter more than the plumbing:

1. **One probe per model.** `generate()` retried a 503 five times and charged
   every retry to flash-latest's 20/day -- [503, 503, 503, 503, 503] in the
   daily logs of 2026-09-25, -26 and -27, a quarter of the tailor budget gone
   before a single document. The chain probes once, benches the model for the
   rest of the run, and moves on.
2. **Only providers that do not train on prompts.** Résumé content goes to
   them; a provider not in llm.PROVIDERS is refused, never quietly sent to
   Gemini as a model name.
"""
import httpx
import pytest

from jobpipe import llm

CHAIN = ["gemini-flash-latest", "groq:qwen/qwen3.8-27b", "gemini-flash-lite-latest"]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A private budget file, generous caps, no real sleeping, keys for Gemini and Groq only."""
    monkeypatch.setattr(llm, "STATE_FILE", tmp_path / "budget.json")
    monkeypatch.setenv("GEMINI_RPM", "10000")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GROQ_API_KEY", "q-key")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    llm.reset_bench()
    yield
    llm.reset_bench()


class Fake:
    """Routes httpx.post by host; each host has a queue of replies (the last one repeats)."""

    def __init__(self, monkeypatch, replies: dict[str, list]):
        self.replies = replies
        self.calls: list[dict] = []
        monkeypatch.setattr(llm.httpx, "post", self.post)

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        host = httpx.URL(url).host
        self.calls.append({"url": url, "host": host, "json": json, "headers": headers or {}, "params": params})
        queue = self.replies[host]
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, Exception):
            raise r
        status, body = r
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    def hosts(self):
        return [c["host"] for c in self.calls]


GEMINI = "generativelanguage.googleapis.com"
GROQ = "api.groq.com"


def gemini_ok(obj: str):
    return (200, {"candidates": [{"content": {"parts": [{"text": obj}]}, "finishReason": "STOP"}]})


def openai_ok(obj: str, finish="stop"):
    return (200, {"choices": [{"message": {"content": obj}, "finish_reason": finish}]})


OVERLOADED = (503, {"error": {"message": "high demand"}})
RATE_LIMITED = (429, {"error": {"message": "slow down"}})


# --- names and the training rule -------------------------------------------

def test_a_plain_name_is_gemini_and_a_prefix_picks_the_provider():
    assert llm.split_model("gemini-flash-latest") == ("gemini", "gemini-flash-latest")
    assert llm.split_model("gemini:gemini-3.6-flash") == ("gemini", "gemini-3.6-flash")
    assert llm.split_model("groq:qwen/qwen3.8-27b") == ("groq", "qwen/qwen3.8-27b")
    assert llm.split_model("ollama:gemma4:31b") == ("ollama", "gemma4:31b"), "only the first colon splits"


@pytest.mark.parametrize("name", ["mistral:ministral-14b-latest", "openrouter:qwen/qwen3.8-27b:free",
                                  "requesty:nvidia/nemotron-3-ultra-550b-a55b"])
def test_a_provider_left_out_for_training_on_prompts_is_refused_not_sent_to_gemini(name):
    with pytest.raises(ValueError, match="do not train on prompts"):
        llm.split_model(name)


def test_the_registry_holds_only_providers_with_a_recorded_no_training_policy():
    assert set(llm.PROVIDERS) == {"groq", "ollama"}
    for spec in llm.PROVIDERS.values():
        assert "no training" in spec["policy"]


# --- the OpenAI-compatible path ---------------------------------------------

def test_groq_gets_the_redacted_prompt_json_mode_and_its_own_token_parameter(monkeypatch):
    fake = Fake(monkeypatch, {GROQ: [openai_ok('{"ok": 1}')]})
    monkeypatch.setattr(llm, "DENY_TERMS", ["Initech"])
    out = llm.generate_json("Reach me at someone@example.com or 9876543210. Works at Initech.",
                            model="groq:qwen/qwen3.8-27b")
    assert out == {"ok": 1}
    call = fake.calls[0]
    assert call["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer q-key"
    assert call["json"]["model"] == "qwen/qwen3.8-27b"
    assert call["json"]["response_format"] == {"type": "json_object"}
    assert "max_completion_tokens" in call["json"]
    sent = call["json"]["messages"][0]["content"]
    assert "someone@example.com" not in sent and "9876543210" not in sent and "Initech" not in sent
    assert "[EMAIL]" in sent and "[PHONE]" in sent and "[REDACTED]" in sent


def test_think_blocks_are_stripped_and_a_truncated_reply_is_an_error(monkeypatch):
    Fake(monkeypatch, {GROQ: [openai_ok('<think>hmm</think>{"ok": 2}')]})
    assert llm.generate_json("p", model="groq:qwen/qwen3.8-27b") == {"ok": 2}
    Fake(monkeypatch, {GROQ: [openai_ok('{"ok": ', finish="length")]})
    with pytest.raises(RuntimeError, match="truncated"):
        llm.generate_json("p", model="groq:qwen/qwen3.8-27b")


def test_a_chain_provider_spends_from_its_own_daily_budget(monkeypatch):
    Fake(monkeypatch, {GROQ: [openai_ok('{"ok": 1}')]})
    before = llm.budget_remaining("groq:qwen/qwen3.8-27b")
    llm.generate_json("p", model="groq:qwen/qwen3.8-27b")
    assert llm.budget_remaining("groq:qwen/qwen3.8-27b") == before - 1
    assert llm.budget_remaining("gemini-flash-latest") == llm._cap("gemini-flash-latest"), "Gemini untouched"


def test_a_prefixed_models_cap_can_be_overridden_with_a_shell_safe_name(monkeypatch):
    monkeypatch.setenv("GEMINI_RPD_BUDGET_groq_qwen_qwen3_8_27b", "7")
    assert llm._cap("groq:qwen/qwen3.8-27b") == 7


# --- the chain ----------------------------------------------------------------

def test_the_first_model_that_answers_wins_and_is_named(monkeypatch):
    fake = Fake(monkeypatch, {GEMINI: [gemini_ok('{"a": 1}')]})
    assert llm.generate_json_chain("p", models=CHAIN) == ({"a": 1}, "gemini-flash-latest")
    assert fake.hosts() == [GEMINI]
    assert fake.calls[0]["headers"]["x-goog-api-key"] == "g-key"
    assert not fake.calls[0]["params"] and "key=" not in fake.calls[0]["url"], "the key never rides in the URL"


def test_a_503_costs_ONE_request_then_the_model_is_benched_for_the_rest_of_the_run(monkeypatch):
    fake = Fake(monkeypatch, {GEMINI: [OVERLOADED], GROQ: [openai_ok('{"b": 2}')]})
    out, used = llm.generate_json_chain("p", models=CHAIN)
    assert (out, used) == ({"b": 2}, "groq:qwen/qwen3.8-27b")
    assert fake.hosts() == [GEMINI, GROQ], "one probe on the 503ing model, not five"
    assert llm.budget_remaining("gemini-flash-latest") == 19, "exactly one slot charged"

    # The next job does not spend another probe on it.
    fake.calls.clear()
    assert llm.generate_json_chain("p", models=CHAIN)[1] == "groq:qwen/qwen3.8-27b"
    assert fake.hosts() == [GROQ]


def test_a_429_benches_the_model_briefly_not_for_the_whole_run(monkeypatch):
    Fake(monkeypatch, {GEMINI: [RATE_LIMITED], GROQ: [openai_ok('{"b": 2}')]})
    llm.generate_json_chain("p", models=CHAIN)
    left = llm._benched["gemini-flash-latest"] - llm.time.time()
    assert 0 < left <= llm.BENCH_SECONDS_BUSY


def test_a_timeout_benches_the_model_too(monkeypatch):
    fake = Fake(monkeypatch, {GEMINI: [httpx.ReadTimeout("slow")], GROQ: [openai_ok('{"b": 2}')]})
    assert llm.generate_json_chain("p", models=CHAIN)[1] == "groq:qwen/qwen3.8-27b"
    assert fake.hosts() == [GEMINI, GROQ]
    assert llm._benched["gemini-flash-latest"] > llm.time.time()


def test_a_provider_without_a_key_is_skipped_without_a_request(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    fake = Fake(monkeypatch, {GEMINI: [OVERLOADED, gemini_ok('{"c": 3}')]})
    assert llm.generate_json_chain("p", models=CHAIN) == ({"c": 3}, "gemini-flash-lite-latest")
    assert GROQ not in fake.hosts()


def test_a_model_with_no_budget_left_is_skipped_without_a_request(monkeypatch):
    monkeypatch.setenv("GEMINI_RPD_BUDGET_gemini-flash-latest", "0")
    fake = Fake(monkeypatch, {GROQ: [openai_ok('{"d": 4}')]})
    assert llm.generate_json_chain("p", models=CHAIN)[1] == "groq:qwen/qwen3.8-27b"
    assert fake.hosts() == [GROQ]


def test_a_bad_key_is_benched_so_every_later_job_does_not_repeat_the_401(monkeypatch):
    fake = Fake(monkeypatch, {GEMINI: [gemini_ok('{"e": 5}')],
                              GROQ: [(401, {"error": {"message": "Invalid API Key"}})]})
    monkeypatch.setenv("GEMINI_RPD_BUDGET_gemini-flash-latest", "0")   # start at Groq
    assert llm.generate_json_chain("p", models=CHAIN)[1] == "gemini-flash-lite-latest"
    fake.calls.clear()
    llm.generate_json_chain("p", models=CHAIN)
    assert GROQ not in fake.hosts()


def test_a_gemini_404_falls_through_instead_of_escaping_the_chain(monkeypatch):
    fake = Fake(monkeypatch, {GEMINI: [(404, {"error": {"message": "model not found"}}), gemini_ok('{"f": 6}')],
                              GROQ: [OVERLOADED]})
    assert llm.generate_json_chain("p", models=CHAIN) == ({"f": 6}, "gemini-flash-lite-latest")
    assert fake.hosts() == [GEMINI, GROQ, GEMINI]


def test_every_model_out_is_QuotaExhausted_so_the_run_stops_for_the_day(monkeypatch):
    Fake(monkeypatch, {GEMINI: [OVERLOADED], GROQ: [RATE_LIMITED]})
    with pytest.raises(llm.QuotaExhausted, match="No model in the chain could answer"):
        llm.generate_json_chain("p", models=CHAIN)


def test_answers_that_are_all_unusable_are_about_the_prompt_not_the_day(monkeypatch):
    """Bad JSON is not a reason to stop tailoring other jobs: RuntimeError, not QuotaExhausted,
    and the models stay available for the next prompt."""
    Fake(monkeypatch, {GEMINI: [gemini_ok("not json")], GROQ: [openai_ok("still not json")]})
    with pytest.raises(RuntimeError) as err:
        llm.generate_json_chain("p", models=CHAIN)
    assert not isinstance(err.value, llm.QuotaExhausted)
    assert llm._benched == {}


# --- provenance ---------------------------------------------------------------

def test_screening_records_the_model_that_answered(monkeypatch):
    from jobpipe import screening

    monkeypatch.setattr(screening, "facts", lambda: {"skills": {}, "roles": [{"facts": [{"text": "Ran Linux fleets", "verified": True}]}]})
    monkeypatch.setattr(screening, "profile", lambda: {"identity": {"years_experience": 4}})
    monkeypatch.setattr(screening, "generate_json_chain",
                        lambda prompt, models, temperature: ({"answers": []}, "groq:qwen/qwen3.8-27b"))
    out = screening.generate_for({"title": "SRE", "company": "Acme", "description": "Linux"})
    assert out["model"] == "groq:qwen/qwen3.8-27b"


# --- review findings, 2026-09-27 (each failed on the first version of the chain) ---

def test_the_LAST_model_standing_keeps_its_retries_so_one_routine_503_does_not_end_the_day(monkeypatch):
    """Review #1. Today's shape (no Groq/Ollama key): flash-latest 503s, then flash-lite
    has one intermittent 503. With one probe each, both were benched and prepare stopped
    with ~480 flash-lite calls unspent. The last resort now gets the full retry loop."""
    monkeypatch.delenv("GROQ_API_KEY")
    fake = Fake(monkeypatch, {GEMINI: [OVERLOADED, OVERLOADED, gemini_ok('{"g": 7}')]})
    assert llm.generate_json_chain("p", models=CHAIN) == ({"g": 7}, "gemini-flash-lite-latest")
    assert fake.hosts() == [GEMINI, GEMINI, GEMINI], "flash-latest once, flash-lite twice"
    assert "gemini-flash-lite-latest" not in llm._benched
    assert "gemini-flash-latest" in llm._benched


def test_a_single_pinned_model_keeps_its_retries(monkeypatch):
    """Review #4: cmd_daily's explicit fallback stage pins one model; it must not be
    one-probe-and-benched, or 7.49's top-up stops after one document."""
    Fake(monkeypatch, {GEMINI: [OVERLOADED, gemini_ok('{"h": 8}')]})
    assert llm.generate_json_chain("p", models=["gemini-flash-lite-latest"]) == ({"h": 8}, "gemini-flash-lite-latest")


def test_a_refused_chain_entry_is_skipped_and_named_never_a_crash(monkeypatch):
    """Review #2: split_model raising out of chain_budget()/cmd_status aborted the whole
    daily run before notify and track."""
    from jobpipe.cli import chain_budget

    bad = ["mistral:ministral-14b-latest", "gemini-flash-latest"]
    assert llm.refusal(bad[0]) and llm.refusal(bad[1]) is None
    assert llm.has_key(bad[0]) is False
    left, parts = chain_budget(bad)
    assert "mistral" not in parts and left == llm.budget_remaining("gemini-flash-latest")
    Fake(monkeypatch, {GEMINI: [gemini_ok('{"i": 9}')]})
    assert llm.generate_json_chain("p", models=bad) == ({"i": 9}, "gemini-flash-latest")


def test_gemini_says_bad_key_with_a_400_and_that_benches_the_model_too(monkeypatch):
    """Review #3: Gemini answers a revoked key with HTTP 400 API_KEY_INVALID, not 401.
    It used to be read as a one-off request error, re-probed on every job."""
    bad_key = (400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                               "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}})
    monkeypatch.delenv("GROQ_API_KEY")
    fake = Fake(monkeypatch, {GEMINI: [bad_key]})
    with pytest.raises(llm.QuotaExhausted, match="bad key"):
        llm.generate_json_chain("p", models=CHAIN)
    assert fake.hosts() == [GEMINI, GEMINI], "each Gemini model once, never retried"
    fake.calls.clear()
    with pytest.raises(llm.QuotaExhausted):
        llm.generate_json_chain("p", models=CHAIN)
    assert fake.calls == [], "both benched: no request on the next job"


def test_an_ordinary_400_is_about_the_request_not_the_key(monkeypatch):
    Fake(monkeypatch, {GEMINI: [(400, {"error": {"message": "Invalid JSON payload received."}})],
                       GROQ: [openai_ok('{"j": 10}')]})
    assert llm.generate_json_chain("p", models=CHAIN)[1] == "groq:qwen/qwen3.8-27b"
    assert "gemini-flash-latest" not in llm._benched


def test_no_sleep_after_the_final_attempt(monkeypatch):
    """Review #6: the old Gemini loop slept 2-3 s after its LAST 503/429 and then raised
    anyway -- minutes per run once the chain probes each model once."""
    naps = []
    monkeypatch.setattr(llm.time, "sleep", naps.append)
    Fake(monkeypatch, {GEMINI: [OVERLOADED]})
    with pytest.raises(llm.QuotaExhausted):
        llm.generate_json("p", model="gemini-flash-latest", max_retries=1)
    assert naps == []
    with pytest.raises(llm.QuotaExhausted):
        llm.generate_json("p", model="gemini-flash-latest", max_retries=3)
    assert len(naps) == 2, "between attempts only"
    naps.clear()
    Fake(monkeypatch, {GEMINI: [RATE_LIMITED]})
    with pytest.raises(llm.QuotaExhausted):
        llm.generate_json("p", model="gemini-flash-latest", max_retries=1)
    assert naps == [], "a 429 on the last attempt does not nap either"


def test_a_mixed_case_prefix_uses_the_same_budget_key_and_cap(monkeypatch):
    """Review #7: 'Groq:...' routed to Groq but counted against a separate key with the
    500 default instead of the 40-call guard."""
    assert llm._canonical("Groq:qwen/qwen3.8-27b") == "groq:qwen/qwen3.8-27b"
    assert llm._canonical("gemini:gemini-flash-lite-latest") == "gemini-flash-lite-latest"
    Fake(monkeypatch, {GROQ: [openai_ok('{"k": 11}')]})
    before = llm.budget_remaining("groq:qwen/qwen3.8-27b")
    assert llm.generate_json_chain("p", models=["Groq:qwen/qwen3.8-27b"])[1] == "groq:qwen/qwen3.8-27b"
    assert llm.budget_remaining("groq:qwen/qwen3.8-27b") == before - 1
    assert llm._cap("groq:qwen/qwen3.8-27b") == 40


def test_the_same_model_named_twice_is_asked_once(monkeypatch):
    fake = Fake(monkeypatch, {GEMINI: [OVERLOADED, OVERLOADED, OVERLOADED, OVERLOADED, OVERLOADED]})
    with pytest.raises(llm.QuotaExhausted):
        llm.generate_json_chain("p", models=["gemini-flash-lite-latest", "gemini:gemini-flash-lite-latest"])
    assert len(fake.calls) == llm.LAST_RESORT_RETRIES, "one model, its full retries, once"


def test_ollama_reads_the_misspelt_OLAMA_API_KEY_the_env_files_were_saved_with(monkeypatch):
    """Both .env files carry OLAMA_API_KEY; reading only OLLAMA_API_KEY skipped Ollama silently."""
    monkeypatch.setenv("OLAMA_API_KEY", "o-key")
    assert llm.has_key("ollama:nemotron-3-ultra")
    fake = Fake(monkeypatch, {"ollama.com": [openai_ok('{"l": 12}')]})
    assert llm.generate_json_chain("p", models=["ollama:nemotron-3-ultra"]) == ({"l": 12}, "ollama:nemotron-3-ultra")
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer o-key"


def test_every_default_chain_model_has_a_budget_cap_sized_for_it():
    """A pinned Gemini Flash model left at the 500 default would let chain_budget promise
    room Google refuses after 20 calls (7.33's failure again)."""
    from jobpipe.config import TAILOR_CHAIN

    for m in TAILOR_CHAIN:
        c = llm._canonical(m)
        if c != "gemini-flash-lite-latest":       # the global 500 default IS its measured cap
            assert c in llm.DEFAULT_MODEL_CAPS, c


def test_every_request_a_provider_receives_is_in_the_usage_log(monkeypatch, tmp_path):
    """The dashboard's 'AI models used' panel reads this log (usage.py)."""
    import json as _json

    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("JOBPIPE_USAGE_LOG", str(log))
    monkeypatch.setenv("GEMINI_RPD_BUDGET_gemini-3.6-flash", "0")      # a spent budget sends nothing
    Fake(monkeypatch, {GEMINI: [OVERLOADED], GROQ: [openai_ok('{"m": 1}')]})
    llm.generate_json_chain("p", models=["gemini-3.6-flash", "gemini-flash-latest", "groq:qwen/qwen3.8-27b"])
    rows = [_json.loads(l) for l in log.read_text().splitlines()]
    assert [(r["provider"], r["model"], r["outcome"]) for r in rows] == [
        ("gemini", "gemini-flash-latest", "overloaded"),
        ("groq", "qwen/qwen3.8-27b", "ok"),
    ]
