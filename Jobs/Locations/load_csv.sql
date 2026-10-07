DROP TABLE IF EXISTS zips;

CREATE TABLE zips (
    zip_code TEXT,
    city TEXT,
    state TEXT,
    county TEXT,
    latitude REAL,
    longitude REAL
);

.mode csv
.import --csv --skip 1 Jobs/Locations/us-zips.csv zips