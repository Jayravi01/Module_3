"""Alert engine: decides which changes need attention (High, or above ALERT_MIN_LEVEL)
and formats them. Each alert is written to the log and to the alerts table; the email
that delivers them is added by the daily scheduler (milestone 3.2).
"""
from __future__ import annotations

import logging

from .classifier import RegulatoryRecord

log = logging.getLogger(__name__)
LEVEL_ORDER = {"Low": 1, "Medium": 2, "High": 3}


def should_alert(rec: RegulatoryRecord, min_level: str) -> bool:
    return LEVEL_ORDER.get(rec.impact_level, 0) >= LEVEL_ORDER.get(min_level, 3)


def format_alert(rec: RegulatoryRecord, source: dict) -> str:
    cats = ", ".join(rec.visa_categories) or "General"
    eff = rec.effective_date.isoformat() if rec.effective_date else "not stated"
    cover = " | ⚕ Affects health cover" if rec.affects_health_cover else ""
    return (f"[{rec.impact_level.upper()} IMPACT] {rec.title}\n"
            f"{rec.change_summary}\n"
            f"Visa categories: {cats} | Effective: {eff}{cover}\n"
            f"Why it matters: {rec.impact_reason}\n"
            f"Source: {source['name']} — {source['url']}")


def send_alert(text: str) -> tuple[str, bool]:
    """Record the alert in the log. Returns (channel, delivered)."""
    log.info("ALERT:\n%s", text)
    return "log", True
