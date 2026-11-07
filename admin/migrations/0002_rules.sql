-- Rule settings and the roasters' crawl flag (docs/design/admin-registry.md §2.2-2.3), and the evidence a decision
-- was made on (group approval by evidence hash). Append-only like every control table: a settings edit is a new
-- version naming the one it replaced, and a crawl flag change is a new row; the latest row is the current value.

-- One row per settings version: the whole document (coffeedata.rules, checked against the deployed code's schema
-- before it is written), what it changed, and which of those changes how a rule decides (CLAUDE.md: those need the
-- full evaluation before Publish, whatever they count).
create table control.rule_settings (
    version_id text primary key,
    prev_id    text references control.rule_settings (version_id),
    schema     int not null,
    doc        jsonb not null,
    changed    jsonb not null default '[]'::jsonb,
    decides    jsonb not null default '[]'::jsonb,
    reason     text not null check (length(reason) between 3 and 500),
    actor      text not null,
    client     text not null default 'ui',
    at         timestamptz not null default now(),
    unique (prev_id)                            -- one successor per version: two tabs can't both edit the same one
);

-- One row per crawl flag change of a roaster. Off: scheduled crawls skip it once a draft carrying it is published.
create table control.roaster_state (
    id         bigserial primary key,
    roaster_id text not null,
    crawl      boolean not null,
    reason     text not null check (length(reason) between 3 and 500),
    actor      text not null,
    client     text not null default 'ui',
    at         timestamptz not null default now()
);
create index roaster_state_roaster on control.roaster_state (roaster_id, id desc);

-- The flags' evidence hash a decision was made on (mart_flags.evidence_hash): one live check of a store covers the
-- removals of a group that share it (design §3, group approval).
alter table control.decisions add column evidence_hash text;

do $$
declare t text;
begin
    foreach t in array array['rule_settings', 'roaster_state'] loop
        execute format('create trigger %I before update or delete or truncate on control.%I '
                       'for each statement '
                       'execute function control.append_only()', t || '_append_only', t);
    end loop;
end $$;
