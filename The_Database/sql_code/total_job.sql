WITH ranked_successful_sources AS (
    SELECT
        company_id,
        ats_vendor,
        job_count,
        ROW_NUMBER() OVER (
            PARTITION BY company_id, ats_vendor
            ORDER BY created_at DESC, source_id DESC
        ) AS row_num
    FROM sources
    WHERE status = 'success'
      AND ats_vendor IS NOT NULL
),
reported_totals AS (
    SELECT
        ats_vendor,
        SUM(COALESCE(job_count, 0)) AS reported_job_count
    FROM ranked_successful_sources
    WHERE row_num = 1
    GROUP BY ats_vendor
),
ranked_company_vendors AS (
    SELECT
        company_id,
        ats_vendor,
        ROW_NUMBER() OVER (
            PARTITION BY company_id
            ORDER BY created_at DESC, source_id DESC
        ) AS row_num
    FROM sources
),
latest_company_vendors AS (
    SELECT company_id, ats_vendor
    FROM ranked_company_vendors
    WHERE row_num = 1
),
posting_totals AS (
    SELECT
        COALESCE(lcv.ats_vendor, 'Unknown') AS ats_vendor,
        COUNT(DISTINCT jp.job_posting_id) AS stored_posting_count
    FROM job_postings AS jp
    LEFT JOIN latest_company_vendors AS lcv
        ON lcv.company_id = jp.company_id
    GROUP BY COALESCE(lcv.ats_vendor, 'Unknown')
),
vendors AS (
    SELECT ats_vendor FROM sources WHERE ats_vendor IS NOT NULL
    UNION
    SELECT ats_vendor FROM posting_totals
)
SELECT
    v.ats_vendor,
    COALESCE(rt.reported_job_count, 0) AS latest_successful_reported_jobs,
    COALESCE(pt.stored_posting_count, 0) AS stored_postings_attributed_to_vendor,
    (SELECT COUNT(*) FROM job_postings) AS total_stored_postings
FROM vendors AS v
LEFT JOIN reported_totals AS rt
    ON rt.ats_vendor = v.ats_vendor
LEFT JOIN posting_totals AS pt
    ON pt.ats_vendor = v.ats_vendor
ORDER BY stored_postings_attributed_to_vendor DESC, v.ats_vendor;

-- The reported count uses only the latest successful scrape per company and
-- ATS. Summing every historical scrape would count the same listings repeatedly.
-- Stored postings are attributed to the ATS in each company's latest source row.
-- Execute with:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/total_job.sql
