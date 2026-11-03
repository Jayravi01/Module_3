"""Alert emails for the Visa & Regulatory Change Monitor (milestone 3.2).

THE BUSINESS PROBLEM
--------------------
The monitor finds visa and regulatory changes every morning, but nobody should have to open
Tableau every day to find out whether something important happened. Email does one job:
"something that may change overseas health cover demand happened; here it is".

DECISIONS (3 Nov)
    * Recipients and Gmail app password: the SAME ALERT_* settings as Modules 1, 2 and 4.
    * Daily email only when a change is High or Medium impact (ALERT_MIN_LEVEL=Medium), or
      when a source NEWLY stops working. Quiet days send nothing (silence = all fine).
    * Weekly insights email after Monday's run: the last 7 days in numbers.
    * Without SMTP settings every email is written to logs/visa_*_preview.html instead
      (preview mode), so you can check it without sending anything.

The functions that BUILD a message are pure (data in, text out) so they are easy to test;
only ``send_email`` talks to the network.
"""
from __future__ import annotations

import csv
import io
import os
import smtplib
import ssl
from collections import Counter
from email.message import EmailMessage
from html import escape
from pathlib import Path

from .env import LOG_DIR

LEVEL_ORDER = {"Low": 1, "Medium": 2, "High": 3}
IMPACT_COLOURS = {"High": "#b42318", "Medium": "#8a5a00", "Low": "#6b7280"}   # same as Module 2
REQUIRED_SETTINGS = ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_TO")
DAILY_PREVIEW, WEEKLY_PREVIEW, FAILURE_PREVIEW = (
    "visa_alert_preview.html", "visa_weekly_preview.html", "visa_failure_preview.html")
CSV_FIELDS = ["record_date", "impact_level", "source_name", "title", "change_summary", "visa_categories",
              "change_type", "effective_date", "affects_health_cover", "impact_reason", "source_url"]
FONT = "font-family:Arial,sans-serif"


def should_alert(rec, min_level: str) -> bool:
    """True when the record's impact is at least ``min_level`` (High > Medium > Low)."""
    return LEVEL_ORDER.get(rec.impact_level, 0) >= LEVEL_ORDER.get(min_level, 2)


def config_from_env(environ: dict | None = None) -> dict | None:
    """SMTP settings (same names as the other modules), or None for preview mode."""
    env = os.environ if environ is None else environ
    if any(not env.get(name) for name in REQUIRED_SETTINGS):
        return None
    user = env["ALERT_SMTP_USER"]
    return {"user": user, "password": env["ALERT_SMTP_PASSWORD"],
            "to": [a.strip() for a in env["ALERT_TO"].split(",") if a.strip()],
            "from": env.get("ALERT_FROM") or user,
            "host": env.get("ALERT_SMTP_HOST", "smtp.gmail.com"),
            "port": int(env.get("ALERT_SMTP_PORT", "465"))}


# --------------------------------------------------------------------------- helpers

def _sorted(rows: list[dict]) -> list[dict]:
    """High first, then Medium, then Low; newest first inside a level."""
    newest_first = sorted(rows, key=lambda r: str(r.get("record_date")), reverse=True)
    return sorted(newest_first, key=lambda r: -LEVEL_ORDER.get(r.get("impact_level"), 0))   # stable sort


def records_csv(rows: list[dict]) -> bytes:
    """The listed changes as CSV. UTF-8 with BOM so Excel shows accents and dashes correctly."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return ("﻿" + out.getvalue()).encode("utf-8")


def _impact_cell(level: str) -> str:
    return (f"<td style='color:{IMPACT_COLOURS.get(level, '#333')};font-weight:bold'>"
            f"{escape(level or '-')}</td>")


def _change_table(rows: list[dict]) -> str:
    html = ["<table border='1' cellpadding='5' style='border-collapse:collapse;font-size:13px'>",
            "<tr style='background:#f2f3f5'><th>Impact</th><th>Source</th><th>What changed</th>"
            "<th>Visa</th><th>Effective</th><th>Health cover</th></tr>"]
    for r in rows:
        source = escape(r.get("source_name") or "-")
        if r.get("source_url"):
            source = f"<a href='{escape(r['source_url'], quote=True)}'>{source}</a>"
        html.append(f"<tr>{_impact_cell(r.get('impact_level'))}<td>{source}</td>"
                    f"<td>{escape(r.get('change_summary') or '')}<br><span style='color:#555'>"
                    f"Why: {escape(r.get('impact_reason') or '-')}</span></td>"
                    f"<td>{escape(r.get('visa_categories') or 'General')}</td>"
                    f"<td>{escape(str(r.get('effective_date') or 'not stated'))}</td>"
                    f"<td>{'Yes' if r.get('affects_health_cover') else 'No'}</td></tr>")
    html.append("</table>")
    return "\n".join(html)


def _page(title: str, body: list[str]) -> str:
    return (f"<html><body style='{FONT}'><h2 style='color:#d90432'>{escape(title)}</h2>"
            + "\n".join(body) + "</body></html>")


# --------------------------------------------------------------------------- daily alert

def build_daily_message(changes: list[dict], new_failures: list[dict], run_label: str,
                        recovered: list[str] | None = None):
    """Return (subject, text, html, rows) or None when nothing needs attention.

    ``changes``: feed rows (storage.feed) that met ALERT_MIN_LEVEL in this run.
    ``new_failures``: sources that worked before and failed now ({key, name, url, error}).
    """
    if not changes and not new_failures:
        return None
    rows = _sorted(changes)
    counts = Counter(r.get("impact_level") for r in rows)
    parts = [f"{counts[level]} {level}" for level in ("High", "Medium", "Low") if counts.get(level)]
    headline = (", ".join(parts) + f" impact change{'s' if len(rows) != 1 else ''}") if rows else ""
    if new_failures:
        failed = f"{len(new_failures)} source{'s' if len(new_failures) != 1 else ''} stopped working"
        headline = f"{headline}; {failed}" if headline else failed
    subject = f"Visa & regulatory monitor: {headline}"

    text = [f"Visa & Regulatory Change Monitor - {run_label}", f"Summary: {headline}", ""]
    html = [f"<p>{escape(run_label)}<br><b>{escape(headline)}</b></p>"]
    if rows:
        html.append("<h3>Changes that may affect overseas health cover demand</h3>" + _change_table(rows))
        text.append("Changes:")
        for r in rows:
            text.append(f"  [{r.get('impact_level')}] {r.get('source_name')}: {r.get('change_summary')} "
                        f"(visa {r.get('visa_categories') or 'General'}, effective "
                        f"{r.get('effective_date') or 'not stated'}) {r.get('source_url') or ''}")
    if new_failures:
        html.append("<h3>Sources that stopped working</h3><ul>")
        text += ["", "Sources that stopped working (worked in an earlier run):"]
        for f in new_failures:
            line = f"{f.get('name') or f.get('key')}: {f.get('error') or 'unavailable'}"
            link = f" <a href='{escape(f.get('url') or '', quote=True)}'>page</a>" if f.get("url") else ""
            html.append(f"<li>{escape(line)}{link}</li>")
            text.append("  " + line)
        html.append("</ul><p style='font-size:13px'>Earlier records from these sources are kept. "
                    "No access protection was bypassed; the source shows as unavailable on the dashboard.</p>")
    if recovered:
        html.append(f"<p>Working again: {escape(', '.join(recovered))}</p>")
        text += ["", "Working again: " + ", ".join(recovered)]
    html.append("<p style='color:#888;font-size:12px'>Every listed change is in the attached CSV and on the "
                "Module 3 dashboard. Impact is rated for overseas health cover (OSHC, OVHC, OWHC).</p>")
    return subject, "\n".join(text), _page("Visa & Regulatory Change Monitor", html), rows


# --------------------------------------------------------------------------- weekly insights

def build_weekly_message(changes: list[dict], runs: list[dict], source_failures: dict[str, int],
                         period_label: str):
    """Return (subject, text, html, rows) for the Monday "last 7 days" email. Always returns a
    message: a quiet week is worth saying too ("no High or Medium changes").

    ``changes``: feed rows of record_type 'change' in the period.
    ``runs``: run_log rows in the period.  ``source_failures``: source name -> failed days.
    """
    rows = _sorted(changes)
    by_impact = Counter(r.get("impact_level") for r in rows)
    by_visa = Counter()
    for r in rows:
        for code in (r.get("visa_categories") or "General").split(", "):
            by_visa[code or "General"] += 1
    by_type = Counter(r.get("change_type") or "other" for r in rows)
    health = sum(1 for r in rows if r.get("affects_health_cover"))
    days_run = len({str(r.get("run_date")) for r in runs})
    cov = [100 * r["core_sources_ok"] / r["core_sources_total"] for r in runs if r.get("core_sources_total")]
    coverage = f"{sum(cov) / len(cov):.0f}%" if cov else "no runs"

    impact_text = ", ".join(f"{by_impact[l]} {l}" for l in ("High", "Medium", "Low") if by_impact.get(l)) or "none"
    subject = f"Visa & regulatory weekly insights {period_label}: {len(rows)} change{'s' if len(rows) != 1 else ''}"
    text = [f"Weekly insights - {period_label}",
            f"Changes detected: {len(rows)} ({impact_text}); {health} affect health cover",
            f"Monitor ran on {days_run} of 7 days; average core source coverage {coverage}", ""]
    html = [f"<p><b>{escape(period_label)}</b></p>",
            "<table cellpadding='6' style='border-collapse:collapse;font-size:14px'>"
            f"<tr><td>Changes detected</td><td><b>{len(rows)}</b> ({escape(impact_text)})</td></tr>"
            f"<tr><td>Affect health cover</td><td><b>{health}</b></td></tr>"
            f"<tr><td>Monitor ran</td><td><b>{days_run} of 7 days</b></td></tr>"
            f"<tr><td>Average core source coverage</td><td><b>{coverage}</b> (target 90%)</td></tr></table>"]
    if by_visa:
        visa_line = ", ".join(f"{code} ({n})" for code, n in by_visa.most_common(6))
        type_line = ", ".join(f"{t.replace('_', ' ')} ({n})" for t, n in by_type.most_common(6))
        html.append(f"<p><b>Visa types most affected:</b> {escape(visa_line)}<br>"
                    f"<b>Types of change:</b> {escape(type_line)}</p>")
        text += [f"Visa types most affected: {visa_line}", f"Types of change: {type_line}", ""]
    important = [r for r in rows if r.get("impact_level") in ("High", "Medium")]
    if important:
        html.append("<h3>High and Medium changes this week</h3>" + _change_table(important))
        text.append("High and Medium changes:")
        text += [f"  [{r.get('impact_level')}] {r.get('record_date')} {r.get('source_name')}: "
                 f"{r.get('change_summary')}" for r in important]
    else:
        html.append("<p>No High or Medium impact changes this week.</p>")
        text.append("No High or Medium impact changes this week.")
    if source_failures:
        fails = ", ".join(f"{name} ({n} day{'s' if n != 1 else ''})" for name, n in
                          sorted(source_failures.items(), key=lambda kv: -kv[1]))
        html.append(f"<p><b>Source health:</b> unavailable on some days: {escape(fails)}</p>")
        text += ["", f"Source health: unavailable on some days: {fails}"]
    else:
        html.append("<p><b>Source health:</b> every source worked on every run.</p>")
        text += ["", "Source health: every source worked on every run."]
    return subject, "\n".join(text), _page("Visa & Regulatory weekly insights", html), rows


# --------------------------------------------------------------------------- sending

def send_email(config: dict, subject: str, text: str, html: str, attachment: bytes | None = None,
               filename: str = "visa_changes.csv") -> None:
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, config["from"], ", ".join(config["to"])
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    if attachment:
        message.add_attachment(attachment, maintype="text", subtype="csv", filename=filename)
    with smtplib.SMTP_SSL(config["host"], config["port"], context=ssl.create_default_context(), timeout=30) as server:
        server.login(config["user"], config["password"])
        server.send_message(message)


def _deliver(message, preview_name: str, filename: str, *, sender, config, log_dir: Path) -> str:
    subject, text, html, rows = message
    attachment = records_csv(rows) if rows else None
    config = config if config is not None else config_from_env()
    if config is None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        (Path(log_dir) / preview_name).write_text(html, encoding="utf-8")
        return "preview"
    sender(config, subject, text, html, attachment, filename)
    return "sent"


def notify_daily(changes, new_failures, run_label, *, recovered=None, sender=send_email, config=None,
                 log_dir: Path = LOG_DIR) -> str:
    """Build and send (or preview) the daily alert. Returns 'sent', 'preview' or 'nothing to send'."""
    message = build_daily_message(changes, new_failures, run_label, recovered)
    if message is None:
        return "nothing to send"
    return _deliver(message, DAILY_PREVIEW, "visa_changes.csv", sender=sender, config=config, log_dir=log_dir)


def notify_weekly(changes, runs, source_failures, period_label, *, sender=send_email, config=None,
                  log_dir: Path = LOG_DIR) -> str:
    message = build_weekly_message(changes, runs, source_failures, period_label)
    return _deliver(message, WEEKLY_PREVIEW, "visa_changes_week.csv", sender=sender, config=config, log_dir=log_dir)


def notify_failure(subject: str, body: str, *, sender=send_email, config=None, log_dir: Path = LOG_DIR) -> str:
    """Plain warning email for a failed or stale run (preview file without SMTP)."""
    config = config if config is not None else config_from_env()
    html = f"<html><body style='{FONT}'><p>{escape(body)}</p></body></html>"
    if config is None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        (Path(log_dir) / FAILURE_PREVIEW).write_text(html, encoding="utf-8")
        return "preview"
    sender(config, subject, body, html, None, "")
    return "sent"
