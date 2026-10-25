# Module 3 — Visa & Regulatory Change Monitor (v10)
**RMIT Capstone Group 4 × Medibank** · Overseas Health market intelligence

Scrapes Australian government visa, migration and education sources every day, detects what
changed, uses GPT to summarise and rate each change for its impact on overseas health cover
demand (OSHC / OVHC / OWHC), stores a permanent day-by-day history in Supabase, raises alerts
for high-impact changes, and feeds Tableau plus a direct CSV export (no approval step).

```
EventBridge Scheduler (7:00 am Melbourne, daily)
        │
        ▼
AWS Lambda ──► scrape gov pages ──► clean text ──► SHA-256 hash
        │                                              │
        │                    first day? ──yes──► GPT baseline summary
        │                       │ no
        │                 same hash? ──yes──► nothing new (history untouched)
        │                       │ no
        │                 line diff vs last good day ──► GPT classification
        │                                              (strict JSON schema)
        ▼                                              │ fallback: rules
Supabase Postgres (append-only) ◄──────────────────────┘
   sources · page_snapshots · regulatory_records · run_log · alerts
        │
        ├──► Tableau (views: v_regulatory_feed, v_records_by_visa, v_daily_kpis)
        ├──► CSV export → S3 (latest + dated archive copy)
        └──► Teams/Slack webhook alert for High impact
```

## Why this design

| Technique | What it solves |
|---|---|
| **Append-only history**, records stamped with the **Melbourne date** | Filter any past day in Tableau and see exactly what was recorded that day |
| **Baseline on day one** | The dashboard isn't empty on the first day; later changes are compared against it |
| **Content normalisation before hashing** (strip scripts, nav, footers, "last updated" stamps) | Stops false "changes" every day from page noise |
| **Diff-only LLM input** | Lower GPT cost; the model focuses on what actually changed |
| **Strict JSON schema output** | Every record has exactly the fields Medibank asked for |
| **Written impact rubric** in the prompt | Consistent High/Medium/Low ratings, tied to health-cover demand |
| **Rule-based fallback** | The daily run never stops if the API is down; records labelled `classifier = rules` |
| **Idempotent runs + retry of failed sources** | Safe to re-run the same day; one outage doesn't lose the day |
| **robots.txt check, polite delay, retries with backoff** | Responsible scraping of government sites |
| **run_log table** | Proves the monitor ran on "no change" days — timeliness evidence |
| **QA columns + sampling script** | Measures accuracy KPIs from human review, not claims |

## Medibank brief → where it's covered

| Brief requirement | Implementation |
|---|---|
| Sources: Home Affairs, Study Australia, Education, Fair Work, gov media releases | `config/sources.yaml` (Phase 1 source inventory) |
| Monitor student, worker, temporary visas, processing times, eligibility, compliance | Visa pages 500/485/482/494/417/600, processing times, news and policy pages; `change_type` field |
| Output: change summary, effective date, impact assessment, visa categories, source link | `regulatory_records` columns + CSV headers |
| Alert categorisation High / Medium / Low | `impact_level` + rubric in `classifier.py`; alerts in `alerts.py` |
| Daily automated refresh | EventBridge Scheduler in `infra/template.yaml` |
| Alerting mechanism | Webhook alerts + `alerts` table |
| Search & filtering by visa type and date | `v_records_by_visa` view; Tableau guide |
| Trend analytics: visa change frequency | Tableau sheet 4 |
| Weekly insights | `scripts/kpi_report.py` scorecard (extendable to a weekly summary) |
| Success question 5: "What visa and regulatory changes could impact future demand?" | Answered directly by the feed + `affects_health_cover` + `effective_date` |

## Run it live (today = day one)

Same steps on **macOS and Windows**. Install Python 3.10+ from python.org first
(Windows: tick "Add python.exe to PATH"). Then, in a terminal in this folder:

```
python setup_project.py --schedule 07:00
```
(On macOS use `python3` if `python` isn't found.)

This creates `.venv`, installs the packages and headless browser, creates `.env`, runs the
code checks, and schedules the monitor every day at 7:00 am (Task Scheduler on Windows,
cron on macOS). Leave out `--schedule` to skip scheduling; remove it later with
`python setup_project.py --unschedule`.

Run it once by hand to capture today:

| macOS | Windows |
|---|---|
| `source .venv/bin/activate` | `.venv\Scripts\activate` |
| `python scripts/run_daily.py` | `python scripts\run_daily.py` |

A ready `.env` file is included (no secrets in it).
Edit `.env`: add `OPENAI_API_KEY`. For Supabase, also set `STORAGE_BACKEND=postgres` and
`DATABASE_URL`, and run `sql/001_schema.sql` once in the Supabase SQL editor. Leave
`STORAGE_BACKEND=sqlite` to keep the history in a local file (`data/visa_monitor.db`).

```bash
python scripts/check_sources.py    # confirm every live source is reachable
python scripts/run_daily.py        # live scrape + store + CSV export
```

Every run writes the CSVs automatically, no approval step:

| File | Contents |
|---|---|
| `exports/regulatory_records.csv` | **Full history**: every baseline and change from day one to today |
| `exports/daily/YYYY-MM-DD.csv` | Only that day's records (one file per day, kept) |
| `exports/run_log.csv` | One row per run: sources checked, failed, changes found |

Day one creates a **baseline** record per source (the state of each page today). From day two,
only real changes are added. Earlier days are never overwritten, so today's rows are still
in tomorrow's CSV and in Tableau when you filter by date.

## Run it every day

`setup_project.py --schedule 07:00` sets this up automatically. The computer must be awake at
that time; on a missed day, just run `python scripts/run_daily.py` by hand.
For a schedule that doesn't depend on anyone's computer, deploy to AWS (`infra/deploy.md`).

Check `logs/monitor.log` if a run looks wrong.

## Measuring the KPIs for the presentation

```bash
python scripts/kpi_report.py                     # coverage, timeliness, usefulness, effort
python scripts/qa_sample.py sample --n 50        # draw changes to check by hand
# ...open each Source Link, mark Y/N in reports/qa_sample.csv...
python scripts/qa_sample.py load reports/qa_sample.csv --reviewer "Sri"
python scripts/kpi_report.py                     # now includes accuracy %
```

Targets come from Medibank's Module 1 framework, which they said applies to all modules:
coverage >90%, accuracy >95%, detection within 24 hours, manual effort reduced ≥75%.
The effort figure is an estimate; its assumptions are printed with the scorecard, so state them
when presenting.


## Moving to Supabase and AWS (new in v10)

Your local history comes with you; nothing is re-scraped or lost.

1. **Get the connection string.** Supabase → your project → **Connect** → **Transaction pooler**
   (port **6543**). Paste it into `.env` as `DATABASE_URL=` (replace `[YOUR-PASSWORD]`).
2. **Check first:** `python scripts/migrate_to_supabase.py --dry-run`
3. **Copy your history:** `python scripts/migrate_to_supabase.py`
   This creates the tables and Tableau views, then copies every source, snapshot, record and
   run. It is safe to run again; nothing gets duplicated.
4. **Switch over:** in `.env` set `STORAGE_BACKEND=postgres`. `run_daily.py`, `kpi_report.py`,
   `show_records.py` and `export_csv.py` now all read and write Supabase.
5. **Build the Lambda package:** `python scripts/build_lambda.py` → `build/lambda.zip`
   (Linux versions of every library, so it runs on Lambda even when built on a Mac).
   Upload steps are in `infra/deploy.md`.

**Supabase pooler fix.** Supabase's pooler can't keep prepared statements, and psycopg 3
starts using them after a query runs 5 times, so earlier versions failed partway through a
run with `prepared statement "_pg3_0" does not exist`. v10 turns prepared statements off,
adds SSL for Supabase hosts, and sets a 15-second connect timeout so a wrong URL fails fast.

## Project layout

```
config/sources.yaml          source inventory (Phase 1)
sql/001_schema.sql           Supabase tables + Tableau views
src/visa_monitor/
  scraper.py                 fetch, robots.txt, retries, normalisation, hashing
  diff.py                    line-level diff between snapshots
  classifier.py              GPT structured output + impact rubric + rules fallback
  pipeline.py                daily run logic (baseline / no change / change)
  storage.py                 Postgres (Supabase) and SQLite back ends
  alerts.py                  High-impact webhook alerts
  export.py                  direct CSV export
  kpi.py                     KPI scorecard
  handler.py                 AWS Lambda entry point (+ S3 CSV export)
scripts/                     run_daily (live), export_csv, kpi_report, qa_sample, check_sources
  migrate_to_supabase.py     copy local SQLite history into Supabase (v10)
  build_lambda.py            build build/lambda.zip for AWS Lambda (v10)
setup_project.py             one-time setup + daily schedule (macOS and Windows)
run_monitor.sh / .bat        daily launchers for cron / Task Scheduler
infra/                       SAM template, deploy guide, Playwright Dockerfile
tableau/TABLEAU_GUIDE.md     dashboard build steps and calculated fields
tests/                       code checks (in-memory only, no data shipped)
exports/, data/              your live run of 12 Oct 2026 (12 baselines, 12/12 sources OK)
CHANGELOG.md                 what changed in each version
```

## Troubleshooting

| Message in the run | Meaning | Fix |
|---|---|---|
| `Very little text extracted` | The page builds its content with JavaScript | Keep `ENABLE_JS_RENDER=true` and run `playwright install chromium`; the scraper switches to the browser automatically |
| `HTTP 403` | The site refused the request | Make sure Google Chrome is installed (the scraper uses it when present), then re-run. Diagnose with `python scripts/debug_source.py <key>` |
| `prepared statement ... does not exist` | Old version on the Supabase pooler | Fixed in v10; make sure you're running this version |
| `connection timeout` / `could not translate host name` | Wrong or incomplete `DATABASE_URL` | Copy the Transaction pooler URI again and check the password |
| `relation "sources" does not exist` | Supabase tables not created yet | Run `python scripts/migrate_to_supabase.py` (it creates them) |
| `baselines: 0` on a second run the same day | Sources already captured today are skipped | Normal; check `exports/daily/<today>.csv` |

Re-running on the same day only retries the sources that failed, so fixing a problem and
running again fills in the gaps without duplicating anything.

## Known limitations (be upfront about these)
- **Processing times:** the Home Affairs page shows figures only after a visa type is chosen in
  its form, so a plain page load captures the form, not the numbers. It's set to priority 2 so it
  doesn't distort the coverage KPI. To track the figures, open the page with browser DevTools →
  Network, pick a visa, copy the data request URL it makes, and add that URL to `sources.yaml`.
- **JS-rendered news list:** needs `ENABLE_JS_RENDER=true` and Playwright installed (on by default
  locally). The basic AWS Lambda zip has no browser; use `infra/Dockerfile.playwright` there.
- **Student Guardian (590):** Home Affairs returned 404 for that page on 12 Oct 2026, so it was removed from the source list.
- **Effective dates** are only filled when the source text states one.
- **Accuracy KPIs** need human review of a sample before they can be reported.
- **Source URLs** should be re-checked periodically; failed sources show up in `run_log` and the coverage KPI.
