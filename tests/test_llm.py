"""Checks for the local LLM path (milestone 3.4). Ollama is never really called here:
``requests`` is replaced by small fakes, so the tests run anywhere in under a second."""
import sys
from datetime import date
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visa_monitor import classifier as C  # noqa: E402
from visa_monitor.config import Settings  # noqa: E402
from visa_monitor.diff import diff_pages  # noqa: E402

SRC = {"key": "s500", "name": "Student visa (subclass 500)", "agency": "Home Affairs",
       "url": "https://example.gov.au/500", "category": "visa_page", "visa_hint": ["500"]}
GOOD = {"title": "OSHC from arrival", "change_summary": "Students must hold OSHC from arrival.",
        "effective_date": "2027-01-01", "visa_categories": ["500"], "change_type": "health_insurance",
        "impact_level": "High", "impact_reason": "Changes cover rules for every student.",
        "affects_health_cover": True, "confidence": 0.9, "is_substantive": True}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


def ollama(monkeypatch, models=("qwen2.5:3b",), answer=None, running=True, sent=None):
    """Fake Ollama: GET /api/tags lists ``models``; POST /api/chat returns ``answer`` as JSON text."""
    import json

    def fake_get(url, timeout, proxies=None):
        if not running:
            raise requests.ConnectionError("refused")
        return FakeResponse({"models": [{"name": m} for m in models]})

    def fake_post(url, json=None, timeout=None, proxies=None):
        if sent is not None:
            sent.update(url=url, body=json)
        return FakeResponse({"message": {"content": __import__("json").dumps(answer or GOOD)}})

    monkeypatch.setattr(C.requests, "get", fake_get)
    monkeypatch.setattr(C.requests, "post", fake_post)


def clf(**kw):
    return C.Classifier(Settings(storage_backend="sqlite", **kw))


CHANGE = diff_pages("Student visa (subclass 500)",
                    "Student visa (subclass 500)\nFrom 1 January 2027 OSHC must start on arrival.")


def test_llm_used_when_ollama_runs_with_the_model(monkeypatch):
    sent = {}
    ollama(monkeypatch, sent=sent)
    c = clf()
    rec = c.classify_change(CHANGE, SRC)
    assert c.label == "qwen2.5:3b" and rec.classifier == "qwen2.5:3b"
    assert rec.change_summary == "Students must hold OSHC from arrival."
    assert rec.effective_date == date(2027, 1, 1) and rec.effective_date_basis == "stated"
    body = sent["body"]
    assert sent["url"].endswith("/api/chat") and body["model"] == "qwen2.5:3b"
    assert body["options"]["temperature"] == 0 and body["format"]["type"] == "object"
    assert "+From 1 January 2027" in body["messages"][1]["content"]       # the diff is sent


@pytest.mark.parametrize("kw, setup, reason", [
    ({}, {"running": False}, "not running"),
    ({}, {"models": ("llama3:8b",)}, "not downloaded"),
    ({"llm_enabled": False}, {}, "switched off"),
])
def test_rules_used_when_llm_unavailable(monkeypatch, kw, setup, reason):
    ollama(monkeypatch, **setup)
    c = clf(**kw)
    ok, why = c.check_llm()
    assert not ok and reason in why
    assert c.classify_change(CHANGE, SRC).classifier == "rules"


def test_availability_checked_once_per_run(monkeypatch):
    calls = []
    ollama(monkeypatch)
    real_get = C.requests.get
    monkeypatch.setattr(C.requests, "get", lambda url, timeout, proxies=None: calls.append(url) or real_get(url, timeout))
    c = clf()
    c.classify_change(CHANGE, SRC)
    c.classify_change(CHANGE, SRC)
    assert len(calls) == 1


def test_baselines_never_use_the_llm(monkeypatch):
    ollama(monkeypatch)
    monkeypatch.setattr(C.requests, "post", lambda *a, **k: pytest.fail("baseline must not call the LLM"))
    rec = clf().summarise_baseline("Student visa (subclass 500) page text", SRC)
    assert rec.classifier == "rules" and rec.change_type == "baseline"


def test_messy_answer_is_cleaned_not_rejected(monkeypatch):
    messy = {**GOOD, "impact_level": "high", "change_type": "Visa Caps", "confidence": 7,
             "effective_date": "next July", "extra": "ignored", "visa_categories": ["subclass 485"]}
    ollama(monkeypatch, answer=messy)
    rec = clf().classify_change(CHANGE, SRC)
    assert rec.classifier == "qwen2.5:3b" and rec.impact_level == "High"
    assert C.clean_llm_answer(messy)["change_type"] == "other"     # unknown type cleaned...
    assert rec.change_type == "health_insurance" and rec.confidence == 1.0   # ...then rules decide
    assert rec.effective_date is None and rec.effective_date_basis == "none"
    assert rec.visa_categories == ["485"] and rec.visa_basis == "inferred"   # 485 not in the changed text


def test_invalid_json_falls_back_to_rules(monkeypatch):
    ollama(monkeypatch)
    monkeypatch.setattr(C.requests, "post",
                        lambda *a, **k: FakeResponse({"message": {"content": "not json at all"}}))
    assert clf().classify_change(CHANGE, SRC).classifier == "rules"


def test_model_name_and_url_come_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434/")
    s = Settings()
    assert s.ollama_model == "qwen2.5:7b" and s.ollama_url == "http://127.0.0.1:11434"


def test_prompt_defines_every_change_type():
    """Small models pick vague types ('policy') unless each type is defined in the instructions."""
    for change_type in C.CHANGE_TYPES:
        assert f"{change_type} = " in C.SYSTEM_PROMPT
    assert C.SYSTEM_PROMPT.index("health_insurance = ") < C.SYSTEM_PROMPT.index("policy = ")


def test_change_type_comes_from_the_rules_not_the_model(monkeypatch):
    ollama(monkeypatch, answer={**GOOD, "change_type": "fees", "affects_health_cover": False})
    rec = clf().classify_change(CHANGE, SRC)
    assert rec.classifier == "qwen2.5:3b"                      # summary/impact still from the AI
    assert rec.change_type == "health_insurance"               # 'OSHC' in the changed line
    assert rec.affects_health_cover is True                    # rules found OSHC even if the AI did not
