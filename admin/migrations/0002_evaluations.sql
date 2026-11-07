-- CLAUDE.md step 9's running total for the coffee classifier's output: the public coffees each published draft's
-- decisions changed, counted since the last full evaluation. Recording one (its eval/ folder at a commit on main)
-- starts the total again. Append-only, like every control table.
create table control.evaluations (
    id     bigserial primary key,
    at     timestamptz not null default now(),
    actor  text not null,
    path   text not null check (path ~ '^eval/[A-Za-z0-9_./-]+$'),
    reason text not null
);
create trigger evaluations_append_only before update or delete or truncate on control.evaluations
    for each statement execute function control.append_only();
