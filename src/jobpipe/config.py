"""Configuration loading. Single source of truth for paths and settings."""
from __future__ import annotations

import os
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

MODEL_SCORE = os.getenv("MODEL_SCORE", "claude-haiku-4-5-20251001")
MODEL_TAILOR = os.getenv("MODEL_TAILOR", "claude-sonnet-5")


def _load(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    return yaml.safe_load(path.read_text()) or {}


@lru_cache(maxsize=None)
def profile() -> dict:
    return _load("profile.yaml")


@lru_cache(maxsize=None)
def facts() -> dict:
    return _load("facts.yaml")


@lru_cache(maxsize=None)
def companies() -> dict:
    return _load("companies.yaml")


def env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)
