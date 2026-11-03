r"""Check the local AI (Ollama) and try it on one sample change.

    python scripts\llm_check.py

Prints whether Ollama is running and the model is downloaded, then classifies a short
sample change (not real data, nothing is stored) and shows the answer and how long it took.
If anything is missing it says what to do; the daily run then uses the keyword rules.
"""
import sys
import time

import _bootstrap  # noqa: F401  (import path, project folder, .env)
from visa_monitor.classifier import Classifier, rules_classify
from visa_monitor.config import Settings
from visa_monitor.diff import diff_pages

SOURCE = {"key": "sample", "name": "Student visa (subclass 500)", "agency": "Department of Home Affairs",
          "url": "https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/student-500",
          "category": "visa_page", "visa_hint": ["500"]}
BEFORE = "Student visa (subclass 500)\nYou must have adequate health insurance while in Australia."
AFTER = (BEFORE + "\nFrom 1 July 2027 your Overseas Student Health Cover (OSHC) must start on the "
         "day you arrive, and the application charge increases to $2,000.")

settings = Settings()
clf = Classifier(settings)
ok, reason = clf.check_llm()
print(f"Ollama address : {settings.ollama_url}")
print(f"Model          : {settings.ollama_model}")
print(f"Status         : {'READY' if ok else 'NOT READY'} - {reason}")
if not ok:
    print("\nFix: install Ollama from https://ollama.com, start it, then run:\n"
          f"     ollama pull {settings.ollama_model}\n"
          "Until then the daily run uses the keyword rules (classifier = rules).")
    sys.exit(1)

diff = diff_pages(BEFORE, AFTER)
start = time.perf_counter()
rec = clf.classify_change(diff, SOURCE)
seconds = time.perf_counter() - start
rules = rules_classify("\n".join(diff.added), SOURCE, "change")

print(f"\nSample change classified in {seconds:.1f} s by: {rec.classifier}")
for label, value in [("Summary", rec.change_summary), ("Impact", f"{rec.impact_level} - {rec.impact_reason}"),
                     ("Change type", rec.change_type), ("Visa", ", ".join(rec.visa_categories) or "General"),
                     ("Effective date", f"{rec.effective_date or 'none'} ({rec.effective_date_basis})"),
                     ("Health cover", rec.affects_health_cover), ("Confidence", rec.confidence)]:
    print(f"  {label:<15}: {value}")
print(f"\nKeyword rules would say: {rules.impact_level}, {rules.change_type}")
print("LLM CHECK OK" if rec.classifier != "rules" else "LLM answer was invalid: rules were used (see logs)")
