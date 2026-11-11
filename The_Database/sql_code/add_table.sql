CREATE TABLE IF NOT EXISTS locations (
    location_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_posting_id  INTEGER NOT NULL,
    city_name       TEXT,
    state_abbv      TEXT,
    country         TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (job_posting_id)
        REFERENCES job_postings(job_posting_id)
        ON UPDATE CASCADE 
        ON DELETE RESTRICT

);


-- EXECUTE COMMANT:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/add_table.sql
