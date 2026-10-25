"""Turn a page (baseline) or a diff (change) into a structured regulatory record.

Primary path: OpenAI GPT with a strict JSON schema, so every response has
exactly the dashboard fields Medibank asked for. Fallback path: deterministic
keyword rules, used when no API key is set or the API call fails, so the daily
run never stops and the record is clearly labelled classifier='rules'.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import Settings

log = logging.getLogger(__name__)

KNOWN_SUBCLASSES = ["500", "590", "485", "482", "494", "186", "189", "190", "491",
                    "417", "462", "600", "408", "400", "407", "820", "309"]
CHANGE_TYPES = ["eligibility", "processing_time", "compliance", "policy", "fees",
                "health_insurance", "work_rights", "caps_and_quotas", "other"]


class RegulatoryRecord(BaseModel):
    title: str = Field(..., max_length=200)
    change_summary: str
    effective_date: date | None = None
    visa_categories: list[str] = []
    change_type: Literal["eligibility", "processing_time", "compliance", "policy", "fees",
                         "health_insurance", "work_rights", "caps_and_quotas", "other"] = "other"
    impact_level: Literal["High", "Medium", "Low"] = "Low"
    impact_reason: str = ""
    affects_health_cover: bool = False
    confidence: float = Field(0.5, ge=0, le=1)
    is_substantive: bool = True     # False => cosmetic edit, not stored as a change
    classifier: str = "rules"

    @field_validator("visa_categories")
    @classmethod
    def _clean_categories(cls, v: list[str]) -> list[str]:
        out = []
        for item in v:
            m = re.search(r"\d{3}", str(item))
            code = m.group(0) if m else str(item).strip()
            if code and code not in out:
                out.append(code)
        return out


IMPACT_RUBRIC = """Impact rubric (Medibank overseas health cover lens — OSHC, OVHC, OWHC):
- High: changes to who can get or keep a visa (eligibility, caps, enrolment limits,
  English/financial requirements), new or changed health-insurance (OSHC/OVHC) conditions,
  visa grant pauses, major fee increases, new compliance obligations — anything that can
  shift the number of students/workers arriving or their cover requirements.
- Medium: processing-time changes, moderate fee changes, work-rights hour changes,
  new guidance that changes how applicants comply, announcements of future reforms.
- Low: wording clarifications, formatting, contact details, links, minor updates
  with no effect on arrivals or cover requirements."""

SYSTEM_PROMPT = f"""You are a regulatory analyst for Medibank's Overseas Health team.
You read Australian government web content about visas, migration, international
education and workplace rules, and you report changes that could affect demand for
overseas health insurance (OSHC for students, OVHC for visitors, OWHC for workers).

{IMPACT_RUBRIC}

Rules:
- Use only facts present in the supplied text. Never invent dates, numbers or subclasses.
- effective_date: ISO date if the text states one, otherwise null.
- visa_categories: subclass numbers (e.g. "500", "485", "482"); empty list if general.
- is_substantive: false if the change is cosmetic (layout, typos, links, dates stamps).
- confidence: your confidence (0-1) that the classification is correct.
- Write the summary in plain business English, 1-3 sentences."""

JSON_SCHEMA = {
    "name": "regulatory_record",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "change_summary", "effective_date", "visa_categories",
                     "change_type", "impact_level", "impact_reason",
                     "affects_health_cover", "confidence", "is_substantive"],
        "properties": {
            "title": {"type": "string"},
            "change_summary": {"type": "string"},
            "effective_date": {"type": ["string", "null"]},
            "visa_categories": {"type": "array", "items": {"type": "string"}},
            "change_type": {"type": "string", "enum": CHANGE_TYPES},
            "impact_level": {"type": "string", "enum": ["High", "Medium", "Low"]},
            "impact_reason": {"type": "string"},
            "affects_health_cover": {"type": "boolean"},
            "confidence": {"type": "number"},
            "is_substantive": {"type": "boolean"},
        },
    },
}


# ------------------------------------------------------------------ rules
HIGH_TERMS = ["cap", "caps", "pause", "suspend", "no longer eligible", "eligibility",
              "ministerial direction", "national planning level", "enrolment limit",
              "genuine student", "english language requirement", "financial capacity",
              "oshc", "overseas student health cover", "health insurance", "visa condition 8501",
              "condition 8501", "will not be granted", "new requirement"]
MEDIUM_TERMS = ["processing time", "processing times", "fee", "fees", "charge",
                "work hours", "hours per fortnight", "work rights", "from 1 ", "will change",
                "changes to", "new guidance", "reform", "consultation"]
HEALTH_TERMS = ["oshc", "ovhc", "health insurance", "health cover", "8501", "medicare"]
DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(20\d{2})\b", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def _find_subclasses(text: str, hints: list[str]) -> list[str]:
    found = [c for c in KNOWN_SUBCLASSES
             if re.search(rf"(subclass\s*{c}\b|\b{c}\s*visa|\(subclass {c}\))", text, re.I)]
    return found or list(hints)


def _first_date(text: str) -> date | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


def rules_classify(text: str, source: dict, mode: str) -> RegulatoryRecord:
    low = text.lower()
    is_proc_page = source.get("category") == "processing_times"
    if any(t in low for t in HIGH_TERMS):
        level, reason = "High", "Mentions eligibility, caps, pauses or health-cover conditions"
    elif is_proc_page or any(t in low for t in MEDIUM_TERMS):
        level, reason = "Medium", "Processing times, fees, work rights or upcoming changes"
    else:
        level, reason = "Low", "No high- or medium-impact terms found"

    if is_proc_page or "processing time" in low:
        ctype = "processing_time"
    elif any(t in low for t in HEALTH_TERMS):
        ctype = "health_insurance"
    elif "fee" in low or "charge" in low:
        ctype = "fees"
    elif "work" in low and "hour" in low:
        ctype = "work_rights"
    elif "eligib" in low:
        ctype = "eligibility"
    else:
        ctype = "policy"

    first_lines = [l for l in text.splitlines() if l.strip()][:3]
    summary = " ".join(first_lines)[:400] or "Content changed."
    if mode == "baseline":
        title = f"Baseline: {source['name']}"
        summary = f"Initial snapshot of {source['name']}. {summary}"
    else:
        title = f"Change detected: {source['name']}"
    return RegulatoryRecord(
        title=title[:200], change_summary=summary,
        effective_date=_first_date(text) if mode == "change" else None,  # baselines describe current state
        visa_categories=_find_subclasses(text, source.get("visa_hint", [])),
        change_type=ctype, impact_level=level if mode == "change" else "Low",
        impact_reason=reason if mode == "change" else "Baseline record (no change yet)",
        affects_health_cover=any(t in low for t in HEALTH_TERMS),
        confidence=0.4, classifier="rules",
    )


# ------------------------------------------------------------------- LLM
class Classifier:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._client = None
        if self.settings.openai_api_key:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.settings.openai_api_key)

    @property
    def uses_llm(self) -> bool:
        return self._client is not None

    def _call_llm(self, user_prompt: str) -> dict:
        resp = self._client.chat.completions.create(   # type: ignore[union-attr]
            model=self.settings.openai_model,
            temperature=0,
            response_format={"type": "json_schema", "json_schema": JSON_SCHEMA},
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_prompt}],
        )
        return json.loads(resp.choices[0].message.content)

    def _run(self, prompt: str, fallback_text: str, source: dict, mode: str) -> RegulatoryRecord:
        if self.uses_llm:
            try:
                data = self._call_llm(prompt)
                if not data.get("effective_date"):
                    data["effective_date"] = None
                rec = RegulatoryRecord(**data, classifier=self.settings.openai_model)
                if mode == "baseline":
                    rec.impact_level = "Low"
                    rec.effective_date = None
                    rec.is_substantive = True
                return rec
            except Exception:
                log.exception("LLM classification failed; using rule-based fallback")
        return rules_classify(fallback_text, source, mode)

    def summarise_baseline(self, text: str, source: dict) -> RegulatoryRecord:
        text = text[: self.settings.llm_max_chars]
        prompt = (f"SOURCE: {source['name']} ({source['agency']})\nURL: {source['url']}\n"
                  f"Visa hints: {', '.join(source.get('visa_hint', [])) or 'none'}\n\n"
                  "This is the FIRST snapshot of this page. Summarise the current key rules "
                  "(eligibility, health-cover requirements, work rights, processing times, fees) "
                  "as a baseline. Set impact_level to Low.\n\nPAGE TEXT:\n" + text)
        return self._run(prompt, text, source, "baseline")

    def classify_change(self, diff, source: dict) -> RegulatoryRecord:
        """`diff` is a visa_monitor.diff.PageDiff."""
        prompt = (f"SOURCE: {source['name']} ({source['agency']})\nURL: {source['url']}\n"
                  f"Visa hints: {', '.join(source.get('visa_hint', [])) or 'none'}\n\n"
                  "Below is a unified diff of the page between the previous snapshot and today "
                  "('-' removed, '+' added). Describe what changed and classify its impact.\n\n"
                  "DIFF:\n" + diff.context)
        # Rules fallback reads the new wording first, then what was removed
        fallback = "\n".join(diff.added) + ("\nRemoved: " + " ".join(diff.removed) if diff.removed else "")
        return self._run(prompt, fallback, source, "change")
