WITH latest_success AS (
    SELECT
        company_id,
        ats_vendor,
        MAX(created_at) AS latest_created_at
    FROM sources
    WHERE status = 'success'
    GROUP BY company_id, ats_vendor
),
latest_vendor_totals AS (
    SELECT
        s.ats_vendor,
        SUM(s.job_count) AS latest_total_jobs
    FROM sources AS s
    JOIN latest_success AS ls
        ON s.company_id = ls.company_id
        AND s.ats_vendor = ls.ats_vendor
        AND s.created_at = ls.latest_created_at
    WHERE s.status = 'success'
    GROUP BY s.ats_vendor
)
SELECT
    ats_vendor,
    latest_total_jobs,
    (SELECT COUNT(*) FROM job_postings) AS total_jobs_in_job_postings
FROM latest_vendor_totals
ORDER BY latest_total_jobs DESC;

-- EXECUTE COMMANT:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/total_job.sql
