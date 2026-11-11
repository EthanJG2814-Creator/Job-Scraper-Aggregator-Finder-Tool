-- location_count.sql
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/location_count.sql
-- Or paste one query at a time into DB Browser for SQLite.
--
-- Queries 9-14 need migrate_add_vendors.sql (or the new Table_Create.sql)
-- to have been run first, so job_postings.source_id and ats_vendors exist.

.headers on
.mode column

-- ===============================================================
-- OVERALL (all vendors combined)
-- ===============================================================

-- 1. Total location rows inserted
SELECT COUNT(*) AS total_location_rows
FROM locations;

-- 2. Overall summary
SELECT
    COUNT(*)                              AS total_location_rows,
    COUNT(DISTINCT job_posting_id)        AS postings_with_locations,
    COUNT(DISTINCT raw_location_id)       AS distinct_raw_locations,
    SUM(is_remote)                        AS remote_rows,
    SUM(is_hybrid)                        AS hybrid_rows,
    SUM(country IS NOT NULL)              AS rows_with_country,
    SUM(country IS NULL)                  AS rows_missing_country,
    SUM(state_abbv IS NOT NULL)           AS rows_with_state,
    SUM(city_name IS NOT NULL)            AS rows_with_city
FROM locations;

-- 3. Postings that have NO location row at all
SELECT COUNT(*) AS postings_without_locations
FROM job_postings jp
WHERE NOT EXISTS (
    SELECT 1 FROM locations l
    WHERE l.job_posting_id = jp.job_posting_id
);

-- 4. Rows inserted per day (shows each run's volume)
SELECT
    date(created_at) AS day,
    COUNT(*)         AS rows_inserted
FROM locations
GROUP BY date(created_at)
ORDER BY day DESC;

-- 5. Locations per posting (how many multi-location jobs you have)
SELECT
    locations_per_posting,
    COUNT(*) AS postings
FROM (
    SELECT job_posting_id, COUNT(*) AS locations_per_posting
    FROM locations
    GROUP BY job_posting_id
)
GROUP BY locations_per_posting
ORDER BY locations_per_posting;

-- 6. Top 25 countries by number of location rows
SELECT
    COALESCE(country, '(unresolved)') AS country,
    COUNT(*)                          AS location_rows,
    COUNT(DISTINCT job_posting_id)    AS postings
FROM locations
GROUP BY country
ORDER BY location_rows DESC
LIMIT 25;

-- 7. Top 25 cities
SELECT
    city_name,
    state_abbv,
    country,
    COUNT(*) AS location_rows
FROM locations
WHERE city_name IS NOT NULL
GROUP BY city_name, state_abbv, country
ORDER BY location_rows DESC
LIMIT 25;

-- 8. Sanity check: location rows pointing to a missing raw_locations row
SELECT COUNT(*) AS orphaned_raw_location_refs
FROM locations l
WHERE l.raw_location_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM raw_locations r
      WHERE r.raw_location_id = l.raw_location_id
  );

-- ===============================================================
-- BY ATS VENDOR
-- ===============================================================

-- 9. Location coverage per vendor: which vendors still need the
--    location step? (postings vs postings that have a location row)
SELECT
    v.vendor_name,
    COUNT(*)                                         AS postings,
    SUM(EXISTS (
        SELECT 1 FROM locations l
        WHERE l.job_posting_id = jp.job_posting_id
    ))                                               AS postings_with_locations,
    COUNT(*) - SUM(EXISTS (
        SELECT 1 FROM locations l
        WHERE l.job_posting_id = jp.job_posting_id
    ))                                               AS postings_without_locations
FROM job_postings jp
JOIN sources s     ON s.source_id = jp.source_id
JOIN ats_vendors v ON v.ats_vendor_id = s.ats_vendor_id
GROUP BY v.vendor_name
ORDER BY postings DESC;

-- 10. Summary per vendor (same columns as query 2, split by vendor)
SELECT
    v.vendor_name,
    COUNT(*)                              AS total_location_rows,
    COUNT(DISTINCT l.job_posting_id)      AS postings_with_locations,
    COUNT(DISTINCT l.raw_location_id)     AS distinct_raw_locations,
    SUM(l.is_remote)                      AS remote_rows,
    SUM(l.is_hybrid)                      AS hybrid_rows,
    SUM(l.country IS NOT NULL)            AS rows_with_country,
    SUM(l.country IS NULL)                AS rows_missing_country,
    ROUND(100.0 * SUM(l.country IS NOT NULL) / COUNT(*), 1) AS pct_with_country
FROM locations l
JOIN job_postings jp ON jp.job_posting_id = l.job_posting_id
JOIN sources s       ON s.source_id = jp.source_id
JOIN ats_vendors v   ON v.ats_vendor_id = s.ats_vendor_id
GROUP BY v.vendor_name
ORDER BY total_location_rows DESC;

-- 11. Rows inserted per day per vendor
SELECT
    date(l.created_at) AS day,
    v.vendor_name,
    COUNT(*)           AS rows_inserted
FROM locations l
JOIN job_postings jp ON jp.job_posting_id = l.job_posting_id
JOIN sources s       ON s.source_id = jp.source_id
JOIN ats_vendors v   ON v.ats_vendor_id = s.ats_vendor_id
GROUP BY date(l.created_at), v.vendor_name
ORDER BY day DESC, rows_inserted DESC;

-- 12. Top 10 countries per vendor
SELECT vendor_name, country, location_rows
FROM (
    SELECT
        v.vendor_name,
        COALESCE(l.country, '(unresolved)') AS country,
        COUNT(*) AS location_rows,
        ROW_NUMBER() OVER (
            PARTITION BY v.vendor_name
            ORDER BY COUNT(*) DESC
        ) AS rn
    FROM locations l
    JOIN job_postings jp ON jp.job_posting_id = l.job_posting_id
    JOIN sources s       ON s.source_id = jp.source_id
    JOIN ats_vendors v   ON v.ats_vendor_id = s.ats_vendor_id
    GROUP BY v.vendor_name, l.country
)
WHERE rn <= 10
ORDER BY vendor_name, location_rows DESC;

-- 13. US vs foreign share per vendor (location rows with a known country)
SELECT
    v.vendor_name,
    SUM(l.country = 'US')                                    AS us_rows,
    SUM(l.country IS NOT NULL AND l.country <> 'US')         AS foreign_rows,
    ROUND(100.0 * SUM(l.country = 'US')
          / NULLIF(SUM(l.country IS NOT NULL), 0), 1)        AS pct_us
FROM locations l
JOIN job_postings jp ON jp.job_posting_id = l.job_posting_id
JOIN sources s       ON s.source_id = jp.source_id
JOIN ats_vendors v   ON v.ats_vendor_id = s.ats_vendor_id
GROUP BY v.vendor_name
ORDER BY us_rows + foreign_rows DESC;

-- 14. Top 50 unresolved location strings per vendor, most frequent first
--     (fix these first; they hurt coverage the most)
SELECT
    v.vendor_name,
    r.raw_text,
    COUNT(*) AS n
FROM locations l
JOIN raw_locations r ON r.raw_location_id = l.raw_location_id
JOIN job_postings jp ON jp.job_posting_id = l.job_posting_id
JOIN sources s       ON s.source_id = jp.source_id
JOIN ats_vendors v   ON v.ats_vendor_id = s.ats_vendor_id
WHERE l.country IS NULL
GROUP BY v.vendor_name, r.raw_text
ORDER BY n DESC
LIMIT 50;

-- execute:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/location_count.sql