"""Configuration loading. Single source of truth for paths and settings.

facts.yaml is schema-validated on load and fails loudly. A typo'd fact ID or a
missing `verified:` key used to drop a fact silently, which looks identical to
"the model chose not to use it" -- the worst possible failure mode for the one
file the whole anti-hallucination gate rests on.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()
# The shared LLM keys file that ContentPipe reads too (~/.config/asitminz/llm.env;
# LLM_SHARED_ENV names another path, "off" skips it), so a provider key is set
# once. It also carries LLM_CATALOG (see llm.PROVIDERS). Loaded after .env and
# never overriding, so the shell, then .env, then this file: .env still wins.
_SHARED_ENV = (os.getenv("LLM_SHARED_ENV") or "").strip()
if _SHARED_ENV != "off":
    load_dotenv(_SHARED_ENV or Path.home() / ".config" / "asitminz" / "llm.env")

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
TEMPLATE_DIR = ROOT / "templates"

for d in (DATA_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("JOBPIPE_DB", DATA_DIR / "jobpipe.db"))

# Gemini model aliases. `make doctor` prints the right values for your key --
# these defaults are the safe free-tier pair. Never point these at a *-preview
# model: separate, much tighter quotas, 429s instantly.
MODEL_SCORE = os.getenv("MODEL_SCORE", "gemini-flash-lite-latest")
MODEL_TAILOR = os.getenv("MODEL_TAILOR", "gemini-flash-latest")
# Tailoring and screening walk this list (llm.generate_json_chain), highest
# intelligence first, one probe each (the last model standing gets retries). A
# model whose provider has no key in .env is skipped. Only providers in
# llm.PROVIDERS can appear (Gemini plus the non-training ones; see there).
#
# Ranked 2026-09-27 by the Artificial Analysis Intelligence Index, read live that
# day (* = from its 2026-09-24 table; those Gemini models have since left the
# board), every entry probed with a live JSON call the same day:
#   gemini-flash-latest (= gemini-3.8-flash that day) 41 | gemini-3.7-flash 39* |
#   groq Qwen3.8 27B 34 (at its highest reasoning setting) | gemini-3.6-flash 34* |
#   gemini-3.5-flash 33* | ollama Nemotron 3 Ultra 23 | gemini-flash-lite-latest
#   (= gemini-3.5-flash-lite) 22 | ollama Gemma 4 31B 19 (SambaNova's copy was added
#   and removed 2026-10-07: its free tier answered 402 "payment method required") | gemini-3.1-flash-lite 16* |
#   zai GLM-4.7-Flash ~15 (current AA index, 2026-10-07) | ollama Nemotron 3 Super 13 | gpt-oss-120b 12 (groq, then ollama).
# zai GLM-4.5-Flash has no score and goes last (2026-10-07).
# Ties go to the faster provider. Re-rank when models change: scores move.
TAILOR_CHAIN = [m.strip() for m in os.getenv(
    "TAILOR_CHAIN",
    ",".join([
        MODEL_TAILOR, "gemini-3.7-flash", "groq:qwen/qwen3.8-27b", "gemini-3.6-flash",
        "gemini-3.5-flash", "ollama:nemotron-3-ultra", "gemini-flash-lite-latest",
        "ollama:gemma4:31b", "gemini-3.1-flash-lite", "zai:glm-4.7-flash", "ollama:nemotron-3-super",
        "groq:openai/gpt-oss-120b", "ollama:gpt-oss:120b", "zai:glm-4.5-flash",
    ]),
).split(",") if m.strip()]


class ConfigError(RuntimeError):
    """Raised when a config file is missing or malformed. Never swallowed."""


def _load(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        example = CONFIG_DIR / name.replace(".yaml", ".example.yaml")
        hint = f"\n  cp {example.relative_to(ROOT)} {path.relative_to(ROOT)}" if example.exists() else ""
        raise ConfigError(f"Missing config file: {path}{hint}")
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{name} is not valid YAML: {e}") from e


@lru_cache(maxsize=None)
def profile() -> dict:
    p = _load("profile.yaml")
    for key in ("identity", "targets", "must_have_any", "thresholds"):
        if key not in p:
            raise ConfigError(f"profile.yaml is missing the '{key}' block")
    return p


# --------------------------------------------------------------------------
# facts.yaml validation -- see module docstring
# --------------------------------------------------------------------------
_FACT_ID = re.compile(r"^[A-Z]\d{3}$")


def _validate_facts(cfg: dict) -> dict:
    seen: dict[str, str] = {}
    problems: list[str] = []

    for section in ("roles", "projects"):
        for group in cfg.get(section) or []:
            label = group.get("name") or group.get("company") or group.get("id") or "<unnamed>"
            if not isinstance(group.get("facts"), list):
                problems.append(f"{section}/{label}: 'facts' must be a list")
                continue
            for i, f in enumerate(group["facts"]):
                where = f"{section}/{label}[{i}]"
                if not isinstance(f, dict):
                    problems.append(f"{where}: expected a mapping, got {type(f).__name__}")
                    continue
                fid = f.get("id")
                if not fid:
                    problems.append(f"{where}: missing 'id'")
                elif not _FACT_ID.match(str(fid)):
                    problems.append(f"{where}: id '{fid}' must match [A-Z]NNN, e.g. F001")
                elif fid in seen:
                    problems.append(f"{where}: duplicate id '{fid}' (also in {seen[fid]})")
                else:
                    seen[str(fid)] = where
                if "verified" not in f:
                    problems.append(f"{where} ({fid}): missing 'verified' key -- add `verified: false`")
                elif not isinstance(f["verified"], bool):
                    problems.append(f"{where} ({fid}): 'verified' must be true or false")
                if not str(f.get("text") or "").strip():
                    problems.append(f"{where} ({fid}): 'text' is empty")

    for i, c in enumerate(cfg.get("certifications") or []):
        if isinstance(c, dict) and "verified" not in c:
            problems.append(f"certifications[{i}]: missing 'verified' key")

    if problems:
        raise ConfigError(
            "config/facts.yaml has {} problem(s) -- fix these, they silently drop facts:\n  - {}"
            .format(len(problems), "\n  - ".join(problems))
        )
    return cfg


@lru_cache(maxsize=None)
def facts() -> dict:
    return _validate_facts(_load("facts.yaml"))


@lru_cache(maxsize=None)
def companies() -> dict:
    return _load("companies.yaml")


def env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default
