"""Print the KPI scorecard and save it to reports/kpi_scorecard.md (+ .json)."""
import json
from pathlib import Path

import _bootstrap
from visa_monitor.kpi import compute_kpis, kpi_markdown

k = compute_kpis(_bootstrap.open_store())
md = kpi_markdown(k)
Path("reports").mkdir(exist_ok=True)
Path("reports/kpi_scorecard.md").write_text(md, encoding="utf-8")
Path("reports/kpi_scorecard.json").write_text(json.dumps(k, indent=2, default=str), encoding="utf-8")
print(md)
