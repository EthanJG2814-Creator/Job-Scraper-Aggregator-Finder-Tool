WITH vendor_companies AS (
    SELECT DISTINCT company_id, ats_vendor
    FROM sources
)
SELECT
    s.ats_vendor,
    COUNT(j.job_posting_id) AS total_jobs,
    SUM(
        CASE
            WHEN j.job_posting_id IS NOT NULL
             AND (
                 j.job_description IS NULL
                 OR TRIM(
                     j.job_description,
                     ' ' || CHAR(9) || CHAR(10) || CHAR(13)
                 ) = ''
             )
            THEN 1
            ELSE 0
        END
    ) AS blank_descriptions
FROM vendor_companies AS s
LEFT JOIN job_postings AS j
    ON j.company_id = s.company_id
GROUP BY s.ats_vendor
ORDER BY blank_descriptions DESC;

-- sqlite3 The_Database/job_data.db < The_Database/sql_code/blank_jobs.sql
