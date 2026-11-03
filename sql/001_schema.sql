-- Module 3 — Visa & Regulatory Change Monitor
-- AWS RDS PostgreSQL, schema "visa" (database medibank). Applied automatically on every
-- run by visa_monitor.db.apply_schema (also: python scripts\db_check.py). The connection's
-- search_path is set to visa, so the names below are created inside that schema.
--
-- Design rule: the pipeline is APPEND-ONLY. Daily runs insert rows stamped
-- with the Melbourne-local date; nothing is updated in place or deleted, so
-- every past day stays available when Tableau filters by date.

-- ---------------------------------------------------------------- sources
create table if not exists sources (
    id          serial primary key,
    key         text not null unique,
    name        text not null,
    agency      text,
    url         text not null unique,
    category    text,
    visa_hint   text[] default '{}',
    render      text default 'static' check (render in ('static', 'js', 'rss')),
    selector    text,
    priority    int  default 1,
    active      boolean default true,
    added_at    timestamptz default now()
);

-- -------------------------------------------------------- page_snapshots
-- One row per source per day. Raw evidence for every change detected.
create table if not exists page_snapshots (
    id              bigserial primary key,
    source_id       int  not null references sources(id),
    snapshot_date   date not null,
    content_hash    text,
    content         text,
    http_status     int,
    fetch_ok        boolean not null,
    error           text,
    scraped_at      timestamptz default now(),
    unique (source_id, snapshot_date)
);

-- ---------------------------------------------------- regulatory_records
-- What the dashboard reads. 'baseline' = state of a page on its first day,
-- 'change' = a real difference detected against the previous snapshot.
create table if not exists regulatory_records (
    id                  bigserial primary key,
    source_id           int  not null references sources(id),
    record_date         date not null,
    record_type         text not null check (record_type in ('baseline', 'change')),
    title               text,
    change_summary      text,
    effective_date      date,
    visa_categories     text[] default '{}',
    change_type         text,
    impact_level        text check (impact_level in ('High', 'Medium', 'Low')),
    impact_reason       text,
    affects_health_cover boolean default false,
    confidence          numeric(3,2),
    classifier          text,           -- e.g. 'gpt-4o-mini' or 'rules'
    source_url          text,
    diff_excerpt        text,
    detected_at         timestamptz default now(),
    -- manual QA columns used for the accuracy KPI (filled by reviewers)
    qa_is_real_change   boolean,
    qa_impact_correct   boolean,
    qa_categories_correct boolean,
    qa_reviewed_by      text,
    unique (source_id, record_date, record_type)
);

-- Milestone 3.3: RSS feeds as a source type, and the "basis" of each fact.
-- (ALTER ... IF NOT EXISTS / DROP IF EXISTS keep this file safe to run on every start.)
alter table sources drop constraint if exists sources_render_check;
alter table sources add constraint sources_render_check check (render in ('static', 'js', 'rss'));
alter table regulatory_records add column if not exists effective_date_basis text;  -- stated / inferred / none
alter table regulatory_records add column if not exists visa_basis           text;
alter table regulatory_records add column if not exists impact_basis         text;

create index if not exists idx_records_date   on regulatory_records(record_date);
create index if not exists idx_records_impact on regulatory_records(impact_level);

-- --------------------------------------------------------------- run_log
create table if not exists run_log (
    run_date            date primary key,
    started_at          timestamptz,
    finished_at         timestamptz,
    sources_checked     int,
    sources_failed      int,
    core_sources_total  int,
    core_sources_ok     int,
    baselines_created   int,
    changes_found       int,
    alerts_sent         int,
    status              text
);

-- ---------------------------------------------------------------- alerts
create table if not exists alerts (
    id          bigserial primary key,
    record_id   bigint references regulatory_records(id),
    alert_date  date not null,
    impact_level text,
    channel     text,
    sent_at     timestamptz default now(),
    delivered   boolean
);

-- ===================================================== views for Tableau
-- Flat, one-row-per-record view: connect Tableau to this.
create or replace view v_regulatory_feed as
select
    r.id,
    r.record_date,
    r.record_type,
    r.title,
    r.change_summary,
    r.effective_date,
    array_to_string(r.visa_categories, ', ') as visa_categories,
    r.change_type,
    r.impact_level,
    case r.impact_level when 'High' then 1 when 'Medium' then 2 else 3 end as impact_rank,
    r.impact_reason,
    r.affects_health_cover,
    r.confidence,
    r.classifier,
    r.source_url,
    s.name   as source_name,
    s.agency as agency,
    s.category as source_category,
    r.detected_at,
    r.effective_date_basis,          -- new columns go LAST: CREATE OR REPLACE VIEW can only append
    r.visa_basis,
    r.impact_basis
from regulatory_records r
join sources s on s.id = r.source_id;

-- One row per record per visa subclass (for the "filter by visa type" view).
create or replace view v_records_by_visa as
select r.id, r.record_date, r.record_type, r.impact_level, r.change_type,
       unnest(case when cardinality(r.visa_categories) = 0
                   then array['General'] else r.visa_categories end) as visa_category,
       s.name as source_name
from regulatory_records r
join sources s on s.id = r.source_id;

-- Daily KPI view: coverage + throughput per run.
create or replace view v_daily_kpis as
select
    l.run_date,
    l.sources_checked,
    l.sources_failed,
    round(100.0 * l.core_sources_ok / nullif(l.core_sources_total, 0), 1) as coverage_pct,
    l.baselines_created,
    l.changes_found,
    l.alerts_sent,
    l.status,
    extract(epoch from (l.finished_at - l.started_at)) as run_seconds
from run_log l;

-- ============================================ views for the Tableau workbook (milestone 3.5)
-- exports/Module_3_Dashboard.twb was built on CSV files. These views return the SAME column
-- names and types as those CSVs, so every sheet, filter and calculated field keeps working
-- when the workbook reads AWS RDS instead. Times are shown in Melbourne time.
create or replace view tableau_records as
select
    r.id::bigint                                         as "Record ID",
    r.record_date                                        as "Record Date",
    r.record_type                                        as "Record Type",
    r.title                                              as "Title",
    r.change_summary                                     as "Change Summary",
    to_char(r.effective_date, 'YYYY-MM-DD')              as "Effective Date",   -- text, as in the CSV
    array_to_string(r.visa_categories, ', ')             as "Visa Categories Affected",
    r.change_type                                        as "Change Type",
    r.impact_level                                       as "Impact Level",
    r.impact_reason                                      as "Impact Assessment",
    coalesce(r.affects_health_cover, false)              as "Affects Health Cover",
    r.confidence::float8                                 as "Confidence",
    r.classifier                                         as "Classifier",
    r.source_url                                         as "Source Link",
    s.name                                               as "Source",
    s.agency                                             as "Agency",
    s.category                                           as "Source Category",
    (r.detected_at at time zone 'Australia/Melbourne')   as "Detected At",
    coalesce(r.effective_date_basis, 'none')             as "Effective Date Basis",
    coalesce(r.visa_basis, 'none')                       as "Visa Basis",
    coalesce(r.impact_basis, 'none')                     as "Impact Basis"
from regulatory_records r
join sources s on s.id = r.source_id;

-- One row per record per numeric visa subclass (records with no subclass have no row).
create or replace view tableau_visa_by_record as
select r.id::bigint as "Record ID", v::bigint as "Visa Subclass"
from regulatory_records r
cross join lateral unnest(r.visa_categories) as v
where v ~ '^[0-9]+$';

-- run_log with start/finish as Melbourne text 'YYYY-MM-DDTHH:MI:SS' (the CSV format the
-- workbook's "Status Line" calculation cuts with LEFT(...,16)).
create or replace view tableau_run_log as
select
    run_date,
    to_char(started_at  at time zone 'Australia/Melbourne', 'YYYY-MM-DD"T"HH24:MI:SS') as started_at,
    to_char(finished_at at time zone 'Australia/Melbourne', 'YYYY-MM-DD"T"HH24:MI:SS') as finished_at,
    sources_checked::bigint as sources_checked, sources_failed::bigint as sources_failed,
    core_sources_total::bigint as core_sources_total, core_sources_ok::bigint as core_sources_ok,
    baselines_created::bigint as baselines_created, changes_found::bigint as changes_found,
    alerts_sent::bigint as alerts_sent, status
from run_log;

-- One row per active source: did its LAST check work? Feeds the "Source status" panel.
--   OK              last fetch worked
--   Scrape blocked  the site refused automated access (HTTP 401/403/429 or robots.txt);
--                   never bypassed, shown as blocked
--   Unavailable     any other failure (timeout, 404, 5xx, page empty)
create or replace view tableau_source_status as
with last_check as (
    select distinct on (source_id) source_id, snapshot_date, fetch_ok, http_status, error
    from page_snapshots
    order by source_id, snapshot_date desc, scraped_at desc
), last_ok as (
    select source_id, max(snapshot_date) as last_ok_date
    from page_snapshots where fetch_ok group by source_id
)
select
    s.name                                                   as "Source",
    s.agency                                                 as "Agency",
    s.category                                               as "Source Category",
    case when s.priority = 1 then 'Core' else 'Secondary' end as "Priority",
    l.snapshot_date                                          as "Last Checked",
    o.last_ok_date                                           as "Last OK",
    case
        when l.source_id is null then 'Not checked yet'
        when l.fetch_ok then 'OK'
        when l.http_status in (401, 403, 429) or l.error ilike '%robots%' then 'Scrape blocked'
        else 'Unavailable'
    end                                                      as "Status",
    coalesce(l.error, '')                                    as "Last Error",
    s.url                                                    as "Source Link"
from sources s
left join last_check l on l.source_id = s.id
left join last_ok o on o.source_id = s.id
where s.active;
