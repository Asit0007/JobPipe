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
