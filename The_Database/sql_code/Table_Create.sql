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

CREATE TABLE IF NOT EXISTS locations (
    location_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_posting_id  INTEGER NOT NULL,
    city_name       TEXT,
    state_abbv      TEXT,
    country         TEXT,
    raw_location_id INTEGER REFERENCES raw_locations(raw_location_id),
    part_index      INTEGER,
    is_remote       INTEGER NOT NULL DEFAULT 0,
    is_hybrid       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (job_posting_id)
        REFERENCES job_postings(job_posting_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT

);

-- EXECUTE COMMANT:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/Table_Create.sql
