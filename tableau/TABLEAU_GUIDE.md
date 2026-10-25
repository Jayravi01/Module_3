# Tableau build guide — Module 3 dashboard

## 1. Connect
**Option A — live database (Tableau Desktop / Tableau Cloud)**
Connect → PostgreSQL → Supabase host, port 5432, database `postgres`, SSL required.
Use the views, not the raw tables:

| View | Use it for |
|---|---|
| `v_regulatory_feed` | Main feed, KPI tiles, impact breakdown (one row per record) |
| `v_records_by_visa` | "Filter by visa type" and visa-category charts (one row per record × visa) |
| `v_daily_kpis` | Coverage %, run status, run duration per day |

On Tableau Cloud, publish the data source as an **extract** and schedule a daily refresh
for 8:00 am Melbourne time (one hour after the 7:00 am Lambda run).

**Option B — CSV (Tableau Public)**
Tableau Public cannot refresh from a database on a schedule. Use the full-history CSV the
Lambda writes to S3 each day (`module3/regulatory_records.csv`), or run
`python scripts/export_csv.py` and reconnect.

## 2. Sheets to build
1. **Change feed** — table of `record_date`, `impact_level`, `title`, `change_summary`,
   `effective_date`, `visa_categories`, `source_name`; sort by `record_date` desc, then
   `impact_rank`. Add a URL action on `source_url` (Dashboard → Actions → Go to URL).
2. **KPI tiles** — Changes this week, High impact this week, Health-cover related, Coverage % (from `v_daily_kpis`).
3. **Impact breakdown** — bar of COUNT(id) by `impact_level`, filtered to `record_type = change`.
4. **Visa change frequency** — line of COUNT(id) by WEEK(`record_date`), from `v_records_by_visa`, colour = `visa_category`.
5. **Changes by type** — bar by `change_type`.
6. **Run health** — `v_daily_kpis` coverage_pct by run_date with a 90% reference line.

## 3. Filters (apply to all sheets)
- `record_date` — **Relative date / range** (this is the history filter: picking yesterday shows yesterday's records)
- `record_type` — baseline / change (default: change)
- `impact_level`, `visa_category`, `agency`, `change_type`

## 4. Calculated fields
```
// Impact colour order
[Impact Sort] = CASE [impact_level] WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END

// Last 7 days flag
[Is Last 7 Days] = [record_date] >= DATEADD('day', -6, TODAY())

// Days until effective
[Days Until Effective] = DATEDIFF('day', TODAY(), [effective_date])

// Upcoming high-impact (for an "on the horizon" panel)
[Upcoming High] = [impact_level] = 'High' AND [effective_date] > TODAY()
```

Colours: High = red, Medium = amber, Low = grey — keep them consistent with Module 1's alert priorities.
