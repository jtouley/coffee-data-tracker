-- The owner's flags on the discovery source list (the admin's Sources page): one row per flag change of a place
-- (its website domain), the latest row per place being its flag. A batch is the places flagged together, undone
-- together. Append-only like every control table. A place flagged `roaster` reaches the registry, and so the probe
-- and the crawl, once a draft carrying it is published (registry.current, dbt stg_registry); `not_roaster` and
-- `unreviewed` never do.
create table control.source_flags (
    id         bigserial primary key,
    batch_id   text not null,
    roaster_id text not null,
    flag       text not null check (flag in ('roaster', 'not_roaster', 'unreviewed')),
    undoes     text,                               -- the batch this row reverts, for an undo
    actor      text not null,
    client     text not null default 'ui',
    at         timestamptz not null default now()
);
create index source_flags_place on control.source_flags (roaster_id, id desc);
create index source_flags_batch on control.source_flags (batch_id);
create trigger source_flags_append_only before update or delete or truncate on control.source_flags
    for each statement execute function control.append_only();
