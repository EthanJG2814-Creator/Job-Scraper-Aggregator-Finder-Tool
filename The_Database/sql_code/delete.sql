DELETE FROM job_postings
WHERE company_id IN (
    SELECT company_id 
    FROM sources 
    WHERE LOWER(ats_vendor) = 'greenhouse'
);


-- EXECUTE COMMAND:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/delete.sql

