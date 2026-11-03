"""Runtime settings, loaded from environment variables (.env, see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES_FILE = PROJECT_ROOT / "config" / "sources.yaml"


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "y"}


@dataclass(frozen=True)
class Settings:
    # Storage: "postgres" (AWS RDS, schema visa) or "sqlite" (offline / tests).
    # Default: postgres when DB_HOST is set in .env, otherwise sqlite.
    storage_backend: str = field(default_factory=lambda: os.getenv(
        "STORAGE_BACKEND", "postgres" if os.getenv("DB_HOST", "").strip() else "sqlite").strip().lower())
    sqlite_path: str = field(default_factory=lambda: os.getenv("SQLITE_PATH", "data/visa_monitor.db"))

    # LLM
    # Local LLM (Ollama). Not running / model missing -> keyword rules, the run continues.
    llm_enabled: bool = field(default_factory=lambda: _env_bool("USE_LLM", True))
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5:7b"))
    llm_timeout: int = field(default_factory=lambda: int(os.getenv("OLLAMA_TIMEOUT", "180")))
    llm_max_chars: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_CHARS", "12000")))

    # Scraping
    user_agent: str = field(default_factory=lambda: os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    ))
    request_timeout: int = field(default_factory=lambda: int(os.getenv("REQUEST_TIMEOUT", "30")))
    polite_delay_seconds: float = field(default_factory=lambda: float(os.getenv("POLITE_DELAY", "2")))
    enable_js_render: bool = field(default_factory=lambda: _env_bool("ENABLE_JS_RENDER", True))

    # Alerts
    alert_min_level: str = field(default_factory=lambda: os.getenv("ALERT_MIN_LEVEL", "Medium"))

    # Time
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Australia/Melbourne"))
    sources_file: str = field(default_factory=lambda: os.getenv("SOURCES_FILE", str(DEFAULT_SOURCES_FILE)))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_sources(path: str | Path | None = None) -> list[dict]:
    """Read the source inventory YAML and return a list of source dicts."""
    path = Path(path or Settings().sources_file)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    sources = data.get("sources", [])
    keys = [s["key"] for s in sources]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate source keys in sources.yaml")
    for s in sources:
        s.setdefault("render", "static")
        s.setdefault("priority", 1)
        s.setdefault("visa_hint", [])
        s.setdefault("selector", None)
    return sources
