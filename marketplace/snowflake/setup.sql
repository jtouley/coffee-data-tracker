-- Snowflake: load the weekly public export from object storage and share it.
-- Prerequisites:
--   * a full (non-trial) account with ACCOUNTADMIN (for the integration and the share)
--   * the weekly job exporting with PUBLIC_EXPORT_URL=s3://<bucket>/coffee-data/
-- Replace <...> placeholders. Run once; the task then reloads weekly.

use role accountadmin;
create database if not exists coffee_data;
create schema if not exists coffee_data.market;
create warehouse if not exists coffee_wh warehouse_size = xsmall auto_suspend = 60 auto_resume = true
  initially_suspended = true;
use schema coffee_data.market;

-- 1. Access to the bucket. After creating it, run DESC INTEGRATION and add Snowflake's IAM user/external id
--    to the role's trust policy.
create storage integration if not exists coffee_s3
  type = external_stage
  storage_provider = 'S3'
  enabled = true
  storage_aws_role_arn = '<arn:aws:iam::123456789012:role/snowflake-coffee-read>'
  storage_allowed_locations = ('s3://<bucket>/coffee-data/');

create file format if not exists parquet_ff type = parquet;
create stage if not exists coffee_public
  url = 's3://<bucket>/coffee-data/'
  storage_integration = coffee_s3
  file_format = parquet_ff;

-- 2. Tables shaped from the files themselves.
create table if not exists public_coffee using template (
  select array_agg(object_construct(*)) from table(infer_schema(
    location => '@coffee_public/public_coffee.parquet', file_format => 'parquet_ff')));
create table if not exists public_roaster using template (
  select array_agg(object_construct(*)) from table(infer_schema(
    location => '@coffee_public/public_roaster.parquet', file_format => 'parquet_ff')));
create table if not exists public_price_event using template (
  select array_agg(object_construct(*)) from table(infer_schema(
    location => '@coffee_public/public_price_event.parquet', file_format => 'parquet_ff')));
create table if not exists public_market_weekly using template (
  select array_agg(object_construct(*)) from table(infer_schema(
    location => '@coffee_public/public_market_weekly.parquet', file_format => 'parquet_ff')));

-- 3. Weekly reload: each table is replaced as a whole, inside one transaction.
--    FORCE = TRUE because the file names are reused every week.
create or replace procedure reload_coffee_public()
returns varchar
language sql
as
$$
begin
  begin transaction;
  truncate table public_coffee;
  copy into public_coffee from @coffee_public/public_coffee.parquet
    match_by_column_name = case_insensitive force = true;
  truncate table public_roaster;
  copy into public_roaster from @coffee_public/public_roaster.parquet
    match_by_column_name = case_insensitive force = true;
  truncate table public_price_event;
  copy into public_price_event from @coffee_public/public_price_event.parquet
    match_by_column_name = case_insensitive force = true;
  truncate table public_market_weekly;
  copy into public_market_weekly from @coffee_public/public_market_weekly.parquet
    match_by_column_name = case_insensitive force = true;
  commit;
  return 'reloaded';
end;
$$;

create or replace task reload_coffee_public_weekly
  warehouse = coffee_wh
  schedule = 'USING CRON 0 14 * * 2 UTC'  -- Tuesdays, after the Monday run has exported
as
  call reload_coffee_public();
alter task reload_coffee_public_weekly resume;
call reload_coffee_public();  -- first load now

-- 4. Share secure views, so table internals can change without breaking consumers.
create schema if not exists coffee_data.share;
create or replace secure view coffee_data.share.coffee as select * from coffee_data.market.public_coffee;
create or replace secure view coffee_data.share.price_event as select * from coffee_data.market.public_price_event;
create or replace secure view coffee_data.share.market_weekly as select * from coffee_data.market.public_market_weekly;
create or replace secure view coffee_data.share.roaster as select * from coffee_data.market.public_roaster;

create share if not exists coffee_data_share;
grant usage on database coffee_data to share coffee_data_share;
grant usage on schema coffee_data.share to share coffee_data_share;
grant select on all views in schema coffee_data.share to share coffee_data_share;

-- 5. Listing: in Snowsight → Data Products → Provider Studio, create a Marketplace listing from
--    coffee_data_share, using the copy in marketplace/LISTING.md. Marketplace listings need a provider
--    profile and acceptance of the Provider and Consumer Terms; Snowflake reviews before it goes live.
