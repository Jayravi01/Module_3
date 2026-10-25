"""Alert engine: pushes High (or above a chosen level) impact changes to a
Microsoft Teams / Slack incoming webhook. With no webhook configured, alerts
are logged only, so the pipeline still records what *would* have been sent.
"""
from __future__ import annotations

import logging

import requests

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


def send_alert(text: str, webhook_url: str) -> tuple[str, bool]:
    if not webhook_url:
        log.info("ALERT (no webhook configured):\n%s", text)
        return "log", True
    try:
        # {"text": ...} is accepted by both Slack and Teams incoming webhooks
        r = requests.post(webhook_url, json={"text": text}, timeout=15)
        return "webhook", r.ok
    except requests.RequestException:
        log.exception("Webhook alert failed")
        return "webhook", False
