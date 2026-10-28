CREATE TABLE IF NOT EXISTS COMPANIES (
    company_id       INTEGER PRIMARY KEY,
    slug_name        TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (slug_name)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id          INTEGER PRIMARY KEY,
    company_id         INTEGER NOT NULL,
    ats_vendor         TEXT,
    status             TEXT,
    job_count          INTEGER,
    error              TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (company_id)
        REFERENCES companies(company_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    UNIQUE (source_id, company_id)
);

CREATE TABLE IF NOT EXISTS job_postings (
    job_posting_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL,   
    job_title       TEXT NOT NULL,
    job_remote      BOOLEAN,
    job_description TEXT NOT NULL,
    published_date  TEXT,
    employment_type TEXT,
    job_url         TEXT,
    compensation_summary    TEXT,
    last_scanned            DATE,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (company_id)
        REFERENCES companies(company_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    UNIQUE (job_url)

);

-- EXECUTE COMMANT:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/Table_Create.sql

