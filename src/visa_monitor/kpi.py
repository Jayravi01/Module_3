"""KPI scorecard against Medibank's success framework
(coverage, accuracy, timeliness, business usefulness).

Every number is computed from the database, so the scorecard in the final
presentation can be traced back to stored evidence.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date

from .storage import BaseStore

TARGETS = {"coverage_pct": 90.0, "accuracy_pct": 95.0, "max_detection_hours": 24, "effort_reduction_pct": 75.0}


def compute_kpis(store: BaseStore, manual_minutes_per_source: float = 5.0,
                 review_minutes_per_change: float = 3.0) -> dict:
    runs = store.runs()
    feed = store.feed()
    changes = [r for r in feed if r["record_type"] == "change"]

    # Coverage — share of core sources fetched successfully, averaged over runs
    cov = [100 * r["core_sources_ok"] / r["core_sources_total"] for r in runs if r["core_sources_total"]]
    coverage = round(sum(cov) / len(cov), 1) if cov else None

    # Timeliness — did the monitor run every day? A daily run => detection < 24 h
    days_run = sorted({str(r["run_date"]) for r in runs})
    if days_run:
        span = (date.fromisoformat(days_run[-1]) - date.fromisoformat(days_run[0])).days + 1
        daily_consistency = round(100 * len(days_run) / span, 1)
    else:
        span, daily_consistency = 0, None

    # Accuracy — from manual QA columns (reviewers mark a sample of records)
    qa = store.query("select qa_is_real_change, qa_impact_correct, qa_categories_correct "
                     "from regulatory_records where record_type='change' and qa_is_real_change is not null")
    def pct(idx):
        vals = [row[idx] for row in qa if row[idx] is not None]
        return round(100 * sum(bool(v) for v in vals) / len(vals), 1) if vals else None

    # Business usefulness — completeness of the 5 required dashboard fields
    required = ["change_summary", "impact_level", "visa_categories", "source_url"]
    complete = [all(r.get(f) for f in required) for r in changes]
    completeness = round(100 * sum(complete) / len(complete), 1) if complete else None
    eff_dates = round(100 * sum(bool(r["effective_date"]) for r in changes) / len(changes), 1) if changes else None

    # Effort — manual daily scan of every source vs reviewing only detected changes
    n_sources = store.query("select count(*) from sources where active = ?", (store._true(),))[0][0]
    manual = manual_minutes_per_source * n_sources * len(days_run)
    automated = review_minutes_per_change * len(changes) + 1.0 * len(days_run)  # 1 min/day dashboard check
    effort = round(100 * (1 - automated / manual), 1) if manual else None

    cats = Counter()
    for r in changes:
        for c in (r["visa_categories"] or "General").split(", "):
            cats[c or "General"] += 1

    return {
        "period": {"first_run": days_run[0] if days_run else None,
                   "last_run": days_run[-1] if days_run else None,
                   "days_in_period": span, "days_run": len(days_run)},
        "coverage": {"avg_core_source_coverage_pct": coverage, "target_pct": TARGETS["coverage_pct"],
                     "sources_monitored": n_sources},
        "accuracy": {"reviewed_changes": len(qa),
                     "detection_accuracy_pct": pct(0), "impact_classification_pct": pct(1),
                     "visa_tagging_pct": pct(2), "target_pct": TARGETS["accuracy_pct"]},
        "timeliness": {"daily_run_consistency_pct": daily_consistency,
                       "refresh_frequency": "Daily (EventBridge Scheduler, Australia/Melbourne)",
                       "max_detection_lag_hours_if_daily": 24},
        "usefulness": {"baselines": sum(r["record_type"] == "baseline" for r in feed),
                       "changes_detected": len(changes),
                       "required_fields_complete_pct": completeness,
                       "effective_date_found_pct": eff_dates,
                       "by_impact": dict(Counter(r["impact_level"] for r in changes)),
                       "by_change_type": dict(Counter(r["change_type"] for r in changes)),
                       "by_visa_category": dict(cats.most_common()),
                       "health_cover_related": sum(r["affects_health_cover"] for r in changes)},
        "effort": {"manual_minutes_estimate": manual, "automated_minutes_estimate": automated,
                   "effort_reduction_pct": effort, "target_pct": TARGETS["effort_reduction_pct"],
                   "assumptions": f"{manual_minutes_per_source} min manual check per source per day; "
                                  f"{review_minutes_per_change} min to review each detected change; "
                                  "1 min daily dashboard check"},
    }


def _status(value, target, higher_is_better=True) -> str:
    if value is None:
        return "⏳ not yet measured"
    ok = value >= target if higher_is_better else value <= target
    return "✅ meets target" if ok else "⚠️ below target"


def _fmt(v) -> str:
    return "—" if v is None else f"{v}%"


def kpi_markdown(k: dict) -> str:
    c, a, t, u, e = k["coverage"], k["accuracy"], k["timeliness"], k["usefulness"], k["effort"]
    lines = [
        "# Module 3 — KPI Scorecard",
        f"Period: {k['period']['first_run']} → {k['period']['last_run']} "
        f"({k['period']['days_run']} runs over {k['period']['days_in_period']} days)\n",
        "| Area | KPI | Result | Target | Status |", "|---|---|---|---|---|",
        f"| Coverage | Core sources successfully monitored | {_fmt(c['avg_core_source_coverage_pct'])} | >{c['target_pct']}% | {_status(c['avg_core_source_coverage_pct'], c['target_pct'])} |",
        f"| Accuracy | Change detection accuracy ({a['reviewed_changes']} reviewed) | {_fmt(a['detection_accuracy_pct'])} | >{a['target_pct']}% | {_status(a['detection_accuracy_pct'], a['target_pct'])} |",
        f"| Accuracy | Impact level correct | {_fmt(a['impact_classification_pct'])} | >{a['target_pct']}% | {_status(a['impact_classification_pct'], a['target_pct'])} |",
        f"| Accuracy | Visa categories correct | {_fmt(a['visa_tagging_pct'])} | >{a['target_pct']}% | {_status(a['visa_tagging_pct'], a['target_pct'])} |",
        f"| Timeliness | Days the monitor ran | {_fmt(t['daily_run_consistency_pct'])} | 100% | {_status(t['daily_run_consistency_pct'], 100)} |",
        f"| Timeliness | Refresh frequency | {t['refresh_frequency']} | Daily | ✅ |",
        f"| Usefulness | Required output fields complete | {_fmt(u['required_fields_complete_pct'])} | 100% | {_status(u['required_fields_complete_pct'], 100)} |",
        f"| Effort | Manual scanning effort reduced | {_fmt(e['effort_reduction_pct'])} | ≥{e['target_pct']}% | {_status(e['effort_reduction_pct'], e['target_pct'])} |",
        "",
        f"**Changes detected:** {u['changes_detected']} (baselines: {u['baselines']}); "
        f"health-cover related: {u['health_cover_related']}",
        f"**By impact:** {json.dumps(u['by_impact'])}",
        f"**By visa category:** {json.dumps(u['by_visa_category'])}",
        f"**By change type:** {json.dumps(u['by_change_type'])}",
        "", f"_Effort assumptions: {e['assumptions']}_",
    ]
    return "\n".join(lines)
