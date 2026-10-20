WITH latest_source AS (
    SELECT
        source_id,
        company_id,
        ats_vendor,
        status,
        job_count,
        error,
        created_at,
        ROW_NUMBER() OVER (
            PARTITION BY company_id
            ORDER BY created_at DESC, source_id DESC
        ) AS rn
    FROM sources
)
SELECT
    ats_vendor,
    error,
    COUNT(*) AS total_errors
FROM latest_source
WHERE rn = 1
  AND error IS NOT NULL
  AND TRIM(error) <> ''
GROUP BY ats_vendor, error
ORDER BY ats_vendor, total_errors DESC;


-- EXECUTE COMMANT:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/error_type.sql

