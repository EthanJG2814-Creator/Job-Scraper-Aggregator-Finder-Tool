CREATE TABLE IF NOT EXISTS raw_locations (
    raw_location_id INTEGER PRIMARY KEY,
    ats_vendor TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    UNIQUE (ats_vendor, raw_text)
);

CREATE TABLE IF NOT EXISTS job_raw_locations (
    job_posting_id INTEGER NOT NULL REFERENCES job_postings(job_posting_id),
    ats_vendor TEXT NOT NULL,
    vendor_job_id TEXT NOT NULL,
    raw_location_id INTEGER NOT NULL REFERENCES raw_locations(raw_location_id),
    workplace_type TEXT,
    PRIMARY KEY (job_posting_id, ats_vendor, raw_location_id)
);
CREATE INDEX IF NOT EXISTS job_raw_locations_raw_idx ON job_raw_locations(raw_location_id);

CREATE TABLE IF NOT EXISTS job_location_fetch_status (
    job_posting_id INTEGER NOT NULL REFERENCES job_postings(job_posting_id),
    ats_vendor TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    PRIMARY KEY (ats_vendor, job_posting_id)
);

CREATE TABLE IF NOT EXISTS locations (
    location_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_posting_id INTEGER NOT NULL REFERENCES job_postings(job_posting_id),
    city_name TEXT,
    state_abbv TEXT,
    country TEXT,
    raw_location_id INTEGER REFERENCES raw_locations(raw_location_id),
    part_index INTEGER,
    is_remote INTEGER NOT NULL DEFAULT 0,
    is_hybrid INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parsed_locations (
    raw_location_id INTEGER PRIMARY KEY REFERENCES raw_locations(raw_location_id),
    parsed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parsed_location_parts (
    raw_location_id INTEGER NOT NULL REFERENCES parsed_locations(raw_location_id),
    part_index INTEGER NOT NULL,
    city_name TEXT,
    state_abbv TEXT,
    country TEXT,
    is_remote INTEGER NOT NULL,
    is_hybrid INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'unresolved')),
    reason TEXT,
    PRIMARY KEY (raw_location_id, part_index)
);

CREATE VIEW IF NOT EXISTS job_parsed_locations AS
SELECT j.job_posting_id, j.ats_vendor, j.vendor_job_id, r.raw_text,
       p.part_index, p.city_name, p.state_abbv, p.country,
       CASE WHEN p.raw_location_id IS NULL THEN NULL
            WHEN j.workplace_type = 'Remote' THEN 1 ELSE p.is_remote END AS is_remote,
       CASE WHEN p.raw_location_id IS NULL THEN NULL
            WHEN j.workplace_type = 'Hybrid' THEN 1 ELSE p.is_hybrid END AS is_hybrid,
       p.status, p.reason
FROM job_raw_locations j
JOIN raw_locations r ON r.raw_location_id = j.raw_location_id
LEFT JOIN parsed_location_parts p ON p.raw_location_id = r.raw_location_id;
