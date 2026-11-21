-- The admin's control plane (docs/design/admin-registry.md §2.2): who did what, the owner's decisions, the job runs
-- the admin started, drafts and the snapshots they were built from. Append-only: every change is a new row, and a
-- trigger refuses UPDATE and DELETE on every table, so the audit trail can't be rewritten through the admin.
-- Applied once, in order, by admin.controlplane.migrate() inside one transaction under an advisory lock.

create table control.audit (
    id         bigserial primary key,
    at         timestamptz not null default now(),
    actor      text not null,
    client     text not null default 'ui',
    action     text not null,                   -- job_start, job_cancel, decide, undo, draft, publish, rollback, ...
    subject    text,                            -- the job, the coffee or hash, the draft, the build
    outcome    text not null check (outcome in ('ok', 'refused', 'failed')),
    detail     jsonb not null default '{}'::jsonb
);
create index audit_at on control.audit (at desc);

-- One row per decision event. A decision (decision_id) is approved once, and may later be reverted (undo); the
-- latest event per subject is the effective one (seq orders events of one decision).
create table control.decisions (
    id           bigserial primary key,
    decision_id  text not null,
    seq          int not null,
    event        text not null check (event in ('approved', 'reverted')),
    subject_kind text not null check (subject_kind in ('coffee_id', 'input_hash')),
    subject      text not null,
    action       text not null check (action in ('include', 'exclude')),
    reason       text not null check (length(reason) between 3 and 500),
    actor        text not null,
    client       text not null default 'ui',
    at           timestamptz not null default now(),
    group_id     text,                           -- decisions approved together (group approval)
    coffee_id    text,                           -- the product the owner decided from (labels, sealed-set checks)
    input_hash   text,
    roaster_id   text,
    title        text,
    unique (decision_id, seq)
);
create index decisions_subject on control.decisions (subject_kind, subject, id desc);

-- A job execution the admin started (Jobs, Release, a live check). Its live status is read from Cloud Run; a final
-- status, once seen, is recorded in job_run_status.
create table control.job_runs (
    run_id          text primary key,
    action          text not null,              -- the registry entry (admin/jobs.py)
    job             text not null,              -- the Cloud Run job
    args            jsonb,                      -- the override, or null for the job's own args
    mode            text,                       -- test or count, for crawls
    week            date,
    test_id         text,
    roaster_id      text,
    execution       text,                       -- projects/.../executions/<name>
    actor           text not null,
    override_reason text,                       -- a scheduled-window override, when given
    started_at      timestamptz not null default now()
);
create index job_runs_started on control.job_runs (started_at desc);
create table control.job_run_status (
    run_id   text primary key references control.job_runs (run_id),
    status     text not null check (status in ('succeeded', 'failed', 'cancelled')),
    duration_s double precision,
    message    text,                            -- Cloud Run's message on a failure
    seen_at    timestamptz not null default now()
);

create table control.snapshots (
    snapshot_id text primary key,
    sha256      text not null,
    decisions   int not null,
    actor       text not null,
    created_at  timestamptz not null default now()
);
create table control.drafts (
    draft_id    text primary key,
    snapshot_id text not null references control.snapshots (snapshot_id),
    baseline    text,                           -- the served build it is built against (null: nothing served)
    run_id      text references control.job_runs (run_id),
    actor       text not null,
    created_at  timestamptz not null default now()
);
create table control.draft_events (
    id       bigserial primary key,
    draft_id text not null references control.drafts (draft_id),
    event    text not null check (event in ('discarded', 'publish_started')),
    actor    text not null,
    at       timestamptz not null default now(),
    detail   jsonb not null default '{}'::jsonb
);

-- A write's idempotency key: a retried or double-submitted form returns the first answer instead of acting twice.
create table control.idempotency (
    key      text primary key,
    at       timestamptz not null default now(),
    response jsonb not null
);

create function control.append_only() returns trigger language plpgsql as $$
begin
    raise exception 'control.% is append-only', tg_table_name;
end $$;
do $$
declare t text;
begin
    foreach t in array array['audit', 'decisions', 'job_runs', 'job_run_status', 'snapshots', 'drafts',
                             'draft_events', 'idempotency'] loop
        execute format('create trigger %I before update or delete or truncate on control.%I '
                       'for each statement '
                       'execute function control.append_only()', t || '_append_only', t);
    end loop;
end $$;
