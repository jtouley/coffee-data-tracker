-- Snowflake, as ACCOUNTADMIN. Loading is done by `coffeedata load-snowflake` (internal stage, no S3).
-- Run part 1 once before the first load, and part 2 after the first load has created the tables.

-- 1. Least-privilege service user for the loader (key-pair auth, no password).
use role accountadmin;
create role if not exists coffee_loader;
create warehouse if not exists coffee_wh warehouse_size = xsmall auto_suspend = 60
  auto_resume = true initially_suspended = true;
create database if not exists coffee_data;
grant usage on warehouse coffee_wh to role coffee_loader;
grant ownership on database coffee_data to role coffee_loader;
create user if not exists claude_coffee type = service
  default_role = coffee_loader default_warehouse = coffee_wh
  rsa_public_key = '<public key body, without the BEGIN/END lines>';
grant role coffee_loader to user claude_coffee;
grant create share on account to role coffee_loader;  -- the share's views are owned by this role
-- Then set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and SNOWFLAKE_PRIVATE_KEY where the loader runs (the GitHub
-- weekly workflow, and/or a Claude environment).

-- 2. Share: secure views over the loaded tables, so table internals can change without breaking consumers.
use role coffee_loader;
create schema if not exists coffee_data.share;
create or replace secure view coffee_data.share.coffee as select * from coffee_data.market.public_coffee;
create or replace secure view coffee_data.share.price_event as select * from coffee_data.market.public_price_event;
create or replace secure view coffee_data.share.market_weekly as select * from coffee_data.market.public_market_weekly;
create or replace secure view coffee_data.share.roaster as select * from coffee_data.market.public_roaster;

create share if not exists coffee_data_share comment = 'US Specialty Coffee Market (weekly)';
grant usage on database coffee_data to share coffee_data_share;
grant usage on schema coffee_data.share to share coffee_data_share;
grant select on all views in schema coffee_data.share to share coffee_data_share;

-- 3. Listing (as ACCOUNTADMIN): Snowsight -> Data Products -> Provider Studio -> create a Marketplace listing from
--    coffee_data_share, using marketplace/LISTING.md. That needs a provider profile and acceptance of the
--    Provider and Consumer Terms, and Snowflake reviews the listing before it goes live. If Provider Studio
--    doesn't offer the share, run as COFFEE_LOADER: grant ownership on share coffee_data_share to role accountadmin;
