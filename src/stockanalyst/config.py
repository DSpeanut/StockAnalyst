"""Settings loader. Reads config/settings.yaml; SEC_USER_AGENT env var overrides sec.user_agent."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, optional quotes. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class SecSettings:
    user_agent: str
    max_requests_per_second: float = 8.0
    forms: tuple[str, ...] = ("8-K", "10-Q", "10-K", "10-K/A", "10-Q/A")


@dataclass(frozen=True)
class Settings:
    sec: SecSettings
    raw_dir: Path = ROOT / "data" / "raw"
    raw: dict = field(default_factory=dict, repr=False)


def load_settings(path: Path | None = None) -> Settings:
    _load_dotenv(ROOT / ".env")
    path = path or ROOT / "config" / "settings.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    sec = raw.get("sec", {})
    user_agent = os.environ.get("SEC_USER_AGENT") or sec.get("user_agent", "")
    if not user_agent or "example.com" in user_agent or "your-name" in user_agent.lower():
        raise ValueError(
            "Set SEC_USER_AGENT (e.g. 'Your Name you@domain.com'); SEC blocks anonymous clients."
        )
    return Settings(
        sec=SecSettings(
            user_agent=user_agent,
            max_requests_per_second=float(sec.get("max_requests_per_second", 8)),
            forms=tuple(sec.get("forms", SecSettings.forms)),
        ),
        raw_dir=ROOT / raw.get("paths", {}).get("raw", "data/raw"),
        raw=raw,
    )
