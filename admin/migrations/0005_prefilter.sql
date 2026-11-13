-- The owner's keyword filters (the admin's Filters page; coffeedata.classify.KeywordFilter) and the
-- owner's review of what a filter change excludes against Claude's verdict (the Release page). Append-only like every
-- control table.

-- One row per change to the filters: a filter added, or removed. A filter is its field and phrase; it is in effect
-- while its latest row is `added`. The row's id is the filters' version: an edit names the version the page showed
-- (a change since is a Conflict). An undo is a row of the opposite event naming the row it reverts (`undoes`).
create table control.keyword_filters (
    id         bigserial primary key,
    event      text not null check (event in ('added', 'removed')),
    phrase     text not null check (length(phrase) between 3 and 60),
    field      text not null check (field in ('title', 'product_type', 'tags')),
    reason     text not null check (length(reason) between 3 and 500),
    undoes     bigint references control.keyword_filters (id),
    actor      text not null,
    client     text not null default 'ui',
    at         timestamptz not null default now()
);
create index keyword_filters_filter on control.keyword_filters (field, phrase, id desc);

-- One row per mark the owner gives a product that a filter change newly excludes while Claude calls it coffee or
-- has no verdict on it (eval_prefilter): keep_excluded, or drop_filter (the filter is wrong for it, so the change
-- can't be published as it is). `filters` is the digest of the filter set the draft carries, so a draft built again
-- on the same filters keeps its marks; the latest mark per (filters, coffee, input_hash) stands, so a mark is about
-- the product version it was made on.
create table control.prefilter_reviews (
    id         bigserial primary key,
    filters    text not null,
    coffee_id  text not null,
    input_hash text,
    prefilter  text,
    mark       text not null check (mark in ('keep_excluded', 'drop_filter')),
    draft_id   text not null,
    actor      text not null,
    client     text not null default 'ui',
    at         timestamptz not null default now()
);
create index prefilter_reviews_set on control.prefilter_reviews (filters, coffee_id, id desc);

do $$
declare t text;
begin
    foreach t in array array['keyword_filters', 'prefilter_reviews'] loop
        execute format('create trigger %I before update or delete or truncate on control.%I '
                       'for each statement '
                       'execute function control.append_only()', t || '_append_only', t);
    end loop;
end $$;
