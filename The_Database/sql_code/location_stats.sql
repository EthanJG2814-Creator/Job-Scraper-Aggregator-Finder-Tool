-- The_Database/sql_code/ats_location_counts.sql
SELECT
    COALESCE(s.ats_vendor, '(unknown)')  AS ats_vendor,
    COUNT(DISTINCT jp.job_posting_id)    AS job_postings,
    COUNT(l.location_id)                 AS total_locations,

    -- Postings that have no rows in the locations table at all
    COUNT(DISTINCT CASE WHEN l.location_id IS NULL
                        THEN jp.job_posting_id END) AS postings_with_no_location_rows,

    -- Location rows that exist but have no city, state, or country
    SUM(CASE WHEN l.location_id IS NOT NULL
              AND COALESCE(TRIM(l.city_name), '')  = ''
              AND COALESCE(TRIM(l.state_abbv), '') = ''
              AND COALESCE(TRIM(l.country), '')    = ''
             THEN 1 ELSE 0 END)          AS location_rows_empty
FROM sources s
JOIN job_postings jp ON jp.company_id = s.company_id
LEFT JOIN locations l ON l.job_posting_id = jp.job_posting_id
GROUP BY COALESCE(s.ats_vendor, '(unknown)')
ORDER BY total_locations DESC;

-- execute:
-- sqlite3 -header -column The_Database/job_data.db < The_Database/sql_code/location_stats.sql