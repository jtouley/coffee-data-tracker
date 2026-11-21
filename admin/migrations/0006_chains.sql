-- The owner's chain flags (the admin's Sources page): one row per change of a registry roaster's chain status, the
-- latest row per roaster being its flag. `chain` and `not_chain` decide over the store count (dbt int_roaster_state:
-- a chain has more than chain_store_threshold stores), either way; `clear` hands the roaster back to its count. A flag
-- reaches the build once a draft carrying it is published (the snapshot's `chains`, dbt var chain_flags); a chain is
-- still crawled, and left out of every public table, the site and the shares. Append-only like every control table.
create table control.chain_flags (
    id         bigserial primary key,
    roaster_id text not null,
    flag       text not null check (flag in ('chain', 'not_chain', 'clear')),
    reason     text not null check (length(reason) between 3 and 500),
    actor      text not null,
    client     text not null default 'ui',
    at         timestamptz not null default now()
);
create index chain_flags_roaster on control.chain_flags (roaster_id, id desc);
create trigger chain_flags_append_only before update or delete or truncate on control.chain_flags
    for each statement execute function control.append_only();

-- The initial flags: dbt/seeds/chain_overrides.csv, the curated chains whose store count reads 20 or fewer, or one
-- over (tests/test_admin_chains.py checks these rows are the seed's).
insert into control.chain_flags (roaster_id, flag, reason, actor, client) values
    ('cariboucoffee.com', 'chain', 'Caribou Coffee runs hundreds of coffeehouses. Overture lists its site on 14 places, because discovery counts only places tagged coffee_roastery or named with roast. A curated chain before the store-count rule.', 'seed', 'migration'),
    ('cleaneatz.com', 'chain', 'Clean Eatz is a restaurant franchise with well over 20 locations. Overture lists its site on 13 roastery places. A curated chain before the store-count rule.', 'seed', 'migration'),
    ('coffeebean.com', 'chain', 'The Coffee Bean & Tea Leaf runs hundreds of US stores. Overture lists its site on 21 roastery places, one above the threshold, so a later Overture release could drop it under. A curated chain before the store-count rule.', 'seed', 'migration'),
    ('peets.com', 'chain', 'Peet''s Coffee runs hundreds of US coffeehouses. The seed lists it from the curated list with 1 location; Overture''s roastery places don''t include its cafes. A curated chain before the store-count rule.', 'seed', 'migration');
