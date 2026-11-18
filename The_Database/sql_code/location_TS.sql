-- The_Database/sql_code/location_samples.sql
-- Change 'Ashby' to any vendor you want to inspect.

.print '=== SAMPLE 1: postings with NO location rows (the 15,332) ==='
SELECT
    jp.job_posting_id,
    c.slug_name,
    jp.job_title,
    jp.job_remote,
    jp.published_date,
    jp.job_url
FROM sources s
JOIN companies c      ON c.company_id = s.company_id
JOIN job_postings jp  ON jp.company_id = s.company_id
LEFT JOIN locations l ON l.job_posting_id = jp.job_posting_id
WHERE s.ats_vendor = 'Ashby'
  AND l.location_id IS NULL
ORDER BY RANDOM()
LIMIT 25;

.print ''
.print '=== SAMPLE 2: location rows that exist but are empty (the 2,112) ==='
SELECT
    l.location_id,
    jp.job_posting_id,
    c.slug_name,
    jp.job_title,
    jp.job_remote,
    l.city_name,
    l.state_abbv,
    l.country,
    l.raw_location_id,
    l.part_index,
    l.is_remote,
    l.is_hybrid,
    jp.job_url
FROM sources s
JOIN companies c      ON c.company_id = s.company_id
JOIN job_postings jp  ON jp.company_id = s.company_id
JOIN locations l      ON l.job_posting_id = jp.job_posting_id
WHERE s.ats_vendor = 'Ashby'
  AND COALESCE(TRIM(l.city_name), '')  = ''
  AND COALESCE(TRIM(l.state_abbv), '') = ''
  AND COALESCE(TRIM(l.country), '')    = ''
  AND l.is_remote = 0
  AND l.is_hybrid = 0
ORDER BY RANDOM()
LIMIT 25;

-- executions
-- sqlite3 -header -column The_Database/job_data.db < The_Database/sql_code/location_TS.sql