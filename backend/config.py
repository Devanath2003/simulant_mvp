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
}


def _keys(provider: str) -> list[str]:
    found = []
    for name, val in ENV.items():
        for pre in PREFIXES[provider]:
            m = re.fullmatch(re.escape(pre) + r"(\d+)", name or "")
            if m and val:
                found.append((int(m.group(1)), val.strip()))
    return [v for _, v in sorted(found)]


KEYS = {p: _keys(p) for p in PREFIXES}

BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}

RUNS_DIR = ROOT / "runs"
