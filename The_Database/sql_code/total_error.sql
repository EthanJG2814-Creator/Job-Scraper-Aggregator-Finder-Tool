WITH latest_source_rows AS (
    SELECT
        s.company_id,
        s.ats_vendor,
        s.status,
        s.source_id
    FROM sources AS s
    WHERE s.source_id = (
        SELECT s2.source_id
        FROM sources AS s2
        WHERE s2.company_id = s.company_id
          AND s2.ats_vendor = s.ats_vendor
        ORDER BY s2.created_at DESC, s2.source_id DESC
        LIMIT 1
    )
)
SELECT
    ats_vendor,
    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS latest_success_count,
    SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) AS latest_fail_count,
    COUNT(*) AS total_companies_checked
FROM latest_source_rows
GROUP BY ats_vendor
ORDER BY ats_vendor;

-- EXECUTE COMMANT:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/total_error.sql

-- column output:
-- ats_vendor|latest_success_count|latest_fail_count|total_companies_checked