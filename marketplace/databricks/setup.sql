-- Databricks (Unity Catalog): load the weekly public export from object storage and share it.
-- Prerequisites:
--   * a Premium workspace with Unity Catalog
--   * an external location (with its storage credential) covering s3://<bucket>/coffee-data/
--   * the weekly job exporting with PUBLIC_EXPORT_URL=s3://<bucket>/coffee-data/
-- Run once, then schedule section 2 as a weekly Databricks Job (Tuesdays) on a SQL warehouse.

-- 1. Catalog and schema.
create catalog if not exists coffee_data;
create schema if not exists coffee_data.market comment 'US specialty coffee market, weekly';

-- 2. Weekly reload: replace each table from this week's files. Replacing keeps the table's identity and
--    history, so shares and grants stay attached.
create or replace table coffee_data.market.coffee
  comment 'One row per coffee ever seen; facts only, links back to the roaster'
  as select * from read_files('s3://<bucket>/coffee-data/public_coffee.parquet', format => 'parquet');
create or replace table coffee_data.market.price_event
  comment 'Weekly price / stock / listing changes'
  as select * from read_files('s3://<bucket>/coffee-data/public_price_event.parquet', format => 'parquet');
create or replace table coffee_data.market.market_weekly
  comment 'Origin x process summary of coffees listed now'
  as select * from read_files('s3://<bucket>/coffee-data/public_market_weekly.parquet', format => 'parquet');
create or replace table coffee_data.market.roaster
  comment 'Roasters covered'
  as select * from read_files('s3://<bucket>/coffee-data/public_roaster.parquet', format => 'parquet');

-- 3. Delta Share (run once).
create share if not exists coffee_data_share comment 'US Specialty Coffee Market (weekly)';
alter share coffee_data_share add table coffee_data.market.coffee;
alter share coffee_data_share add table coffee_data.market.price_event;
alter share coffee_data_share add table coffee_data.market.market_weekly;
alter share coffee_data_share add table coffee_data.market.roaster;

-- 4. Listing: Marketplace → Provider console. Private-exchange listings are self-serve for account
--    admins; public Marketplace listings require joining the Databricks Data Partner Program. Use the copy
--    in marketplace/LISTING.md.
