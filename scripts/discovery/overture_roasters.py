"""Pull US coffee roasters from Overture Maps Places (CDLA-Permissive-2.0) with DuckDB.

usage: python overture_roasters.py [RELEASE] > overture_domains.csv
Reads ~a few hundred MB via HTTP range requests; no AWS credentials needed.
Output: one row per website domain (multi-location roasters collapse to one row).
"""
import re, sys, urllib.request
import duckdb

BUCKET = "https://overturemaps-us-west-2.s3.amazonaws.com/"
# Platform/aggregator domains that are never a roaster's own site.
NOT_A_ROASTER_SITE = (
    "facebook.com|instagram.com|yelp.com|linktr.ee|toasttab.com|google.com|business.site|square.site|"
    "squareup.com|order.online|doordash.com|ubereats.com|grubhub.com|tiktok.com|twitter.com|x.com|"
    "linkedin.com|tripadvisor.com|mapquest.com|youtube.com|clover.com|wixsite.com|godaddysites.com|"
    "weebly.com|blogspot.com|wordpress.com"
)


def list_keys(prefix):
    xml = urllib.request.urlopen(f"{BUCKET}?list-type=2&prefix={prefix}").read().decode()
    return re.findall(r"<Key>([^<]+)</Key>", xml)


def latest_release():
    xml = urllib.request.urlopen(f"{BUCKET}?list-type=2&prefix=release/&delimiter=/").read().decode()
    return sorted(re.findall(r"<Prefix>release/([^<]+)/</Prefix>", xml))[-1]


def main(release):
    urls = [BUCKET + k for k in list_keys(f"release/{release}/theme=places/type=place/") if k.endswith(".parquet")]
    c = duckdb.connect()
    c.sql("INSTALL httpfs; LOAD httpfs;")
    c.sql(f"""
    CREATE TABLE r AS
    SELECT names.primary AS name, taxonomy.primary AS cat, addresses[1].locality AS city,
           addresses[1].region AS state, confidence,
           regexp_replace(lower(regexp_extract(websites[1], '^(?:https?://)?([^/?#]+)', 1)),
                          '^(www|locations|shop)\\.', '') AS domain
    FROM read_parquet({urls})
    WHERE ((bbox.xmin BETWEEN -125 AND -66 AND bbox.ymin BETWEEN 24 AND 50)     -- lower 48
        OR (bbox.xmin BETWEEN -170 AND -129 AND bbox.ymin BETWEEN 51 AND 72)    -- Alaska
        OR (bbox.xmin BETWEEN -161 AND -154 AND bbox.ymin BETWEEN 18 AND 23))   -- Hawaii
      AND addresses[1].country = 'US'
      AND (taxonomy.primary = 'coffee_roastery'
           OR list_contains(taxonomy.alternates, 'coffee_roastery')
           OR (names.primary ILIKE '%roast%'
               AND (taxonomy.primary ILIKE '%coffee%' OR basic_category ILIKE '%coffee%'
                    OR names.primary ILIKE '%coffee%')))""")
    c.sql(f"""
    COPY (
      SELECT arg_max(name, confidence) AS name, domain,
             arg_max(city, confidence) AS city, arg_max(state, confidence) AS state,
             'overture:{release}' AS source, count(*) AS locations,
             round(max(confidence), 2) AS confidence,
             bool_or(cat = 'coffee_roastery') AS tagged_roastery,
             bool_or(name ILIKE '%roast%') AS roast_in_name
      FROM r
      WHERE domain <> '' AND NOT regexp_matches(domain, '(^|\\.)({NOT_A_ROASTER_SITE})$')
      GROUP BY domain ORDER BY state, name
    ) TO '/dev/stdout' (HEADER)""")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else latest_release())
