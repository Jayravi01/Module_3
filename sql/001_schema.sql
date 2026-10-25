-- Module 3 — Visa & Regulatory Change Monitor
-- Supabase / PostgreSQL schema. Run once in the Supabase SQL editor.
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
    render      text default 'static' check (render in ('static', 'js')),
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
    r.detected_at
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
