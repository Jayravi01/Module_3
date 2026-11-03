"""Turn a page (baseline) or a diff (change) into a structured regulatory record.

Primary path for CHANGES: a local LLM (Ollama, model qwen2.5:3b) asked to answer in a
fixed JSON schema, so every response has exactly the dashboard fields Medibank asked for.
Nothing leaves the laptop and there is no API cost. Fallback: deterministic keyword rules,
used when Ollama is not running, the model is not downloaded, or its answer is invalid,
so the daily run never stops; such records are labelled classifier='rules'.
BASELINES (first snapshot of a page) always use the rules (decision 3 Nov).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Literal

import requests
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
    # "baseline" is set by apply_basis for first snapshots (a baseline is not a change)
    change_type: Literal["eligibility", "processing_time", "compliance", "policy", "fees",
                         "health_insurance", "work_rights", "caps_and_quotas", "other",
                         "baseline"] = "other"
    impact_level: Literal["High", "Medium", "Low"] = "Low"
    impact_reason: str = ""
    affects_health_cover: bool = False
    confidence: float = Field(0.5, ge=0, le=1)
    is_substantive: bool = True     # False => cosmetic edit, not stored as a change
    classifier: str = "rules"
    # Was each fact STATED in the source text, INFERRED by the monitor, or not present (none)?
    effective_date_basis: Literal["stated", "inferred", "none"] = "none"
    visa_basis: Literal["stated", "inferred", "none"] = "none"
    impact_basis: Literal["stated", "inferred", "none"] = "none"

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
- change_type: pick the MOST SPECIFIC one (the first that fits, in this order):
    health_insurance = OSHC, OVHC, OWHC, health cover, visa condition 8501, Medicare;
    caps_and_quotas = caps, planning levels, enrolment limits, allocation numbers;
    eligibility = who can apply or keep a visa (age, English, funds, genuine student);
    fees = visa application charges or other fees;
    work_rights = allowed work hours or work conditions;
    processing_time = how long applications take;
    compliance = new obligations, checks, penalties, conditions to follow;
    policy = a general policy announcement that fits none of the above;
    other = anything else.
- is_substantive: false if the change is cosmetic (layout, typos, links, dates stamps).
- confidence: your confidence (0-1) that the classification is correct.
- Write the summary in plain business English, 1-3 sentences."""

# The answer format Ollama must follow ("structured outputs": the model can only produce
# JSON that matches this schema). pydantic (RegulatoryRecord) checks it again afterwards.
LLM_SCHEMA = {
    "type": "object",
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


def find_subclasses(text: str) -> list[str]:
    """Visa subclasses the text EXPLICITLY names ("subclass 500", "500 visa", "(subclass 500)")."""
    return [c for c in KNOWN_SUBCLASSES
            if re.search(rf"(subclass\s*{c}\b|\b{c}\s*visa|\(subclass {c}\))", text, re.I)]


def has_term(low: str, term: str) -> bool:
    """Whole-word match: 'cap' matches 'visa cap' but NOT 'capacity' or 'capital'."""
    return re.search(rf"(?<!\w){re.escape(term.strip())}(?!\w)", low) is not None


def all_dates(text: str) -> list[date]:
    """Every date written like '1 July 2026' in the text, in order."""
    out = []
    for m in DATE_RE.finditer(text):
        try:
            out.append(date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))))
        except ValueError:
            continue
    return out


def _first_date(text: str) -> date | None:
    dates = all_dates(text)
    return dates[0] if dates else None


def apply_basis(rec: "RegulatoryRecord", mode: str, text: str, source: dict) -> "RegulatoryRecord":
    """Final, shared rules for BOTH classifiers (LLM and keyword rules).

    ``text`` is what changed (added + removed lines) for a change, or the page for a baseline.

    Visa tags (decision 3 Nov: "hints + changed lines"):
      * baseline -> the page's own visa (visa_hint in sources.yaml), e.g. 500 for the Student
        page; news pages have no hint -> no tag ("General").
      * change   -> subclasses named in the changed lines; if none, the page's own visa.
    Basis (3 columns): 'stated' = written in the source text, 'inferred' = decided by the
    monitor, 'none' = no value.
    """
    hints = [str(h) for h in (source.get("visa_hint") or [])]
    mentioned = find_subclasses(text)
    if mode == "baseline":
        rec.visa_categories = hints
        rec.visa_basis = "none" if not hints else ("stated" if all(h in mentioned for h in hints) else "inferred")
        rec.effective_date, rec.effective_date_basis = None, "none"
        rec.impact_level, rec.impact_basis, rec.change_type = "Low", "none", "baseline"
        return rec
    if not rec.visa_categories:
        rec.visa_categories = mentioned or hints
    codes = rec.visa_categories
    rec.visa_basis = "none" if not codes else ("stated" if all(c in mentioned for c in codes) else "inferred")
    if rec.effective_date is None:
        rec.effective_date_basis = "none"
    else:
        rec.effective_date_basis = "stated" if rec.effective_date in all_dates(text) else "inferred"
    rec.impact_basis = "inferred"          # impact is always the monitor's judgement (rubric)
    return rec


def rules_classify(text: str, source: dict, mode: str) -> RegulatoryRecord:
    low = text.lower()
    is_proc_page = source.get("category") == "processing_times"
    if any(has_term(low, t) for t in HIGH_TERMS):
        level, reason = "High", "Mentions eligibility, caps, pauses or health-cover conditions"
    elif is_proc_page or any(has_term(low, t) for t in MEDIUM_TERMS):
        level, reason = "Medium", "Processing times, fees, work rights or upcoming changes"
    else:
        level, reason = "Low", "No high- or medium-impact terms found"

    if is_proc_page or "processing time" in low:
        ctype = "processing_time"
    elif any(has_term(low, t) for t in HEALTH_TERMS):
        ctype = "health_insurance"
    elif has_term(low, "fee") or has_term(low, "fees") or has_term(low, "charge"):
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
        visa_categories=find_subclasses(text),          # final tags: apply_basis
        change_type=ctype, impact_level=level if mode == "change" else "Low",
        impact_reason=reason if mode == "change" else "Baseline record (no change yet)",
        affects_health_cover=any(has_term(low, t) for t in HEALTH_TERMS),
        confidence=0.4, classifier="rules",
    )


# ------------------------------------------------------------------- LLM
# Ollama runs on this computer: never send its traffic through a proxy set in the environment.
LOCAL = {"http": None, "https": None}


def clean_llm_answer(data: dict) -> dict:
    """Make a small model's JSON safe for RegulatoryRecord: keep only known keys, fix casing,
    turn unknown change types into 'other', clamp confidence to 0..1, and drop a date that is
    not a real YYYY-MM-DD date (a wrong date is worse than no date)."""
    keys = set(LLM_SCHEMA["properties"])
    out = {k: v for k, v in (data or {}).items() if k in keys}
    level = str(out.get("impact_level", "Low")).strip().capitalize()
    out["impact_level"] = level if level in ("High", "Medium", "Low") else "Low"
    ctype = str(out.get("change_type", "other")).strip().lower().replace(" ", "_")
    out["change_type"] = ctype if ctype in CHANGE_TYPES else "other"
    try:
        out["confidence"] = min(1.0, max(0.0, float(out.get("confidence", 0.5))))
    except (TypeError, ValueError):
        out["confidence"] = 0.5
    raw_date = out.get("effective_date")
    try:
        out["effective_date"] = date.fromisoformat(str(raw_date)[:10]) if raw_date else None
    except ValueError:
        out["effective_date"] = None
    out["title"] = str(out.get("title") or "Change detected")[:200]
    out["visa_categories"] = [str(v) for v in (out.get("visa_categories") or [])]
    return out


class Classifier:
    """Rates changes with the local LLM when it is available, otherwise with the rules."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._available: bool | None = None       # checked once, on first use

    # -- is Ollama there? ---------------------------------------------------
    def check_llm(self) -> tuple[bool, str]:
        """(available, reason). Ollama must be running AND the model downloaded."""
        if not self.settings.llm_enabled:
            return False, "LLM switched off (USE_LLM=false)"
        try:
            r = requests.get(f"{self.settings.ollama_url}/api/tags", timeout=5, proxies=LOCAL)
            r.raise_for_status()
        except requests.RequestException:
            return False, f"Ollama is not running at {self.settings.ollama_url}"
        names = {m.get("name", "") for m in r.json().get("models", [])}
        wanted = self.settings.ollama_model
        if wanted not in names and f"{wanted}:latest" not in names:
            return False, f"model {wanted} not downloaded (run: ollama pull {wanted})"
        return True, f"Ollama {wanted}"

    @property
    def uses_llm(self) -> bool:
        if self._available is None:
            self._available, reason = self.check_llm()
            log.info("Classifier: %s", reason if self._available else f"keyword rules ({reason})")
        return self._available

    @property
    def label(self) -> str:
        return self.settings.ollama_model if self.uses_llm else "rules"

    # -- one LLM call ---------------------------------------------------------
    def _call_llm(self, user_prompt: str) -> dict:
        """POST to Ollama's chat API. temperature 0 = the same input gives the same answer."""
        body = {"model": self.settings.ollama_model, "stream": False, "format": LLM_SCHEMA,
                "options": {"temperature": 0},
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user_prompt}]}
        r = requests.post(f"{self.settings.ollama_url}/api/chat", json=body,
                          timeout=self.settings.llm_timeout, proxies=LOCAL)
        r.raise_for_status()
        return json.loads(r.json()["message"]["content"])

    def _run(self, prompt: str, fallback_text: str, source: dict, mode: str) -> RegulatoryRecord:
        if mode == "change" and self.uses_llm:
            try:
                data = clean_llm_answer(self._call_llm(prompt))
                rec = RegulatoryRecord(**data, classifier=self.settings.ollama_model)
                # Decision 3 Nov: the change TYPE comes from the deterministic keyword rules (a
                # small model picked 'policy' or 'fees' for an OSHC change); the AI writes the
                # summary, impact and reason. Health cover = AI OR rules (never miss an OSHC hit).
                rules = rules_classify(fallback_text, source, mode)
                rec.change_type = rules.change_type
                rec.affects_health_cover = rec.affects_health_cover or rules.affects_health_cover
                return apply_basis(rec, mode, fallback_text, source)
            except Exception:
                log.exception("LLM classification failed; using rule-based fallback")
        return apply_basis(rules_classify(fallback_text, source, mode), mode, fallback_text, source)

    def summarise_baseline(self, text: str, source: dict) -> RegulatoryRecord:
        """First snapshot of a page: always the rules (no AI), impact Low."""
        return self._run("", text[: self.settings.llm_max_chars], source, "baseline")
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
