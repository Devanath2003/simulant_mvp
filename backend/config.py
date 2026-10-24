"""Loads API keys from .env. Keys are grouped per provider and handed out round-robin."""
import itertools
import os
import re
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV = {**dotenv_values(ROOT / ".env"), **os.environ}

# .env has a typo (GORQ_API_1) -> accept both spellings
PREFIXES = {
    "groq": ("GROQ_API_", "GORQ_API_"),
    "nvidia": ("NVIDIA_API_",),
    "gemini": ("GEMINI_API_",),
    "openai": ("OPENAI_API_", "OPEN_AI_"),
}
SINGLE = {"openai": ("OPEN_AI", "OPENAI_API_KEY", "OPENAI_API")}   # single-key names, no number suffix


def _keys(provider: str) -> list[str]:
    found = []
    for name, val in ENV.items():
        for pre in PREFIXES[provider]:
            m = re.fullmatch(re.escape(pre) + r"(\d+)", name or "")
            if m and val:
                found.append((int(m.group(1)), val.strip()))
        if name in SINGLE.get(provider, ()) and val:
            found.append((0, val.strip()))
    return list(dict.fromkeys(v for _, v in sorted(found)))


KEYS = {p: _keys(p) for p in PREFIXES}

BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openai": "https://api.openai.com/v1",
}

RUNS_DIR = ROOT / "runs"

# paid (OpenAI) calls allowed per run, and how many of those are kept for the vision navigator. ~$0.0007 per call.
PAID_CALLS_PER_RUN = int(ENV.get("OPENAI_MAX_CALLS_PER_RUN") or 60)
PAID_RESERVE = min(25, PAID_CALLS_PER_RUN)
