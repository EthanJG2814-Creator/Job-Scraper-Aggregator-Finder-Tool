"""Store raw ATS locations and parse distinct location strings offline."""

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata

from logging_errors import Error_Log_system


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = Path(__file__).with_name("location_schema.sql")
DEFAULT_DB = ROOT / "The_Database" / "job_data.db"
DEFAULT_GAZETTEER = Path(__file__).with_name("cities15000.txt")
SPLIT = re.compile(r"\s*[;|]\s*")
REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
PREFIX = re.compile(r"^(?:100%\s*)?(?:remote|hybrid)\s*(?:[-,:]\s*)?", re.IGNORECASE)
ZIP = re.compile(r"\s+\d{5}(?:-\d{4})?$")
HYPHEN_FORMAT = re.compile(r"^(US|USA)-([A-Za-z]{2})-(.+)$", re.IGNORECASE)
WORKDAY_LOCATION_COUNT = re.compile(r"^\s*(\d+)\s+locations?\s*$", re.IGNORECASE)
REMOTE_SUFFIX = re.compile(r"(?:,\s*|\s*-\s*)(?:remote|hybrid)$", re.IGNORECASE)
STREET_ADDRESS = re.compile(r"^\d+\s+\S")
CANADA_ADMIN = {
    "01": "AB", "02": "BC", "03": "MB", "04": "NB", "05": "NL",
    "07": "NS", "08": "ON", "09": "PE", "10": "QC", "11": "SK",
    "12": "YT", "13": "NT", "14": "NU",
}


def key(text):
    normalized = unicodedata.normalize("NFKD", text).casefold()
    return "".join(char for char in normalized if not unicodedata.combining(char)).strip()


def initialize_location_schema(connection):
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'job_raw_locations'"
    ).fetchone():
        link_columns = {row[1] for row in connection.execute("PRAGMA table_info(job_raw_locations)")}
        if "workplace_type" not in link_columns:
            connection.execute("DROP VIEW IF EXISTS job_parsed_locations")
            connection.execute("ALTER TABLE job_raw_locations ADD COLUMN workplace_type TEXT")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    columns = {row[1] for row in connection.execute("PRAGMA table_info(locations)")}
    additions = {
        "raw_location_id": "INTEGER REFERENCES raw_locations(raw_location_id)",
        "part_index": "INTEGER",
        "is_remote": "INTEGER NOT NULL DEFAULT 0",
        "is_hybrid": "INTEGER NOT NULL DEFAULT 0",
    }
    with connection:
        for column, definition in additions.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE locations ADD COLUMN {column} {definition}")
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS locations_raw_part_idx
            ON locations(job_posting_id, raw_location_id, part_index)
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS locations_raw_idx ON locations(raw_location_id)")


def insert_clean_locations(connection, source):
    """Materialize cached resolved parts for the selected posting/raw pairs."""
    if source == "jobs":
        selected = """
            JOIN job_postings j ON j.job_posting_id = l.job_posting_id
            JOIN location_stage_jobs s ON s.url = j.job_url
        """
    elif source == "raw":
        selected = "JOIN location_parse_batch b ON b.raw_location_id = l.raw_location_id"
    else:
        raise ValueError(f"Unknown location selection: {source}")
    connection.execute(f"""
        INSERT INTO locations
            (job_posting_id, city_name, state_abbv, country, raw_location_id,
             part_index, is_remote, is_hybrid)
        SELECT l.job_posting_id, p.city_name, p.state_abbv, p.country,
               p.raw_location_id, p.part_index,
               CASE WHEN l.workplace_type = 'Remote' THEN 1 ELSE p.is_remote END,
               CASE WHEN l.workplace_type = 'Hybrid' THEN 1 ELSE p.is_hybrid END
        FROM job_raw_locations l
        {selected}
        JOIN parsed_location_parts p ON p.raw_location_id = l.raw_location_id
        WHERE p.status = 'resolved'
        ON CONFLICT(job_posting_id, raw_location_id, part_index)
        DO UPDATE SET is_remote = excluded.is_remote, is_hybrid = excluded.is_hybrid
    """)


def greenhouse_raw_locations(job):
    """Return original names and any issues that make the response incomplete."""
    values = []
    issues = []
    for field in ("location", "locations"):
        if field not in job or job[field] is None:
            continue
        locations = job[field] if isinstance(job[field], list) else [job[field]]
        for location in locations:
            if not isinstance(location, dict) or not isinstance(location.get("name"), str):
                issues.append(f"{field} contains a missing or invalid name: {location!r}")
            elif not location["name"].strip():
                issues.append(f"{field} contains a blank name")
            elif location["name"] not in values:
                values.append(location["name"])
    if not values and not issues:
        issues.append("No location name was supplied")
    return values, issues


def ashby_raw_locations(job):
    """Extract Ashby's primary and secondary location strings unchanged."""
    values, issues = [], []

    def add_location(value, field):
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{field} contains a missing or invalid location: {value!r}")
        elif value not in values:
            values.append(value)

    add_location(job.get("location"), "location")
    secondary = job.get("secondaryLocations")
    if secondary is not None:
        if not isinstance(secondary, list):
            issues.append(f"secondaryLocations is not a list: {secondary!r}")
        else:
            for entry in secondary:
                if isinstance(entry, dict):
                    add_location(entry.get("location"), "secondaryLocations")
                else:
                    issues.append(f"secondaryLocations contains an invalid entry: {entry!r}")
    return values, issues


def lever_raw_locations(job):
    """Extract Lever's primary location and allLocations without changing text."""
    categories = job.get("categories")
    if not isinstance(categories, dict):
        return [], [f"categories is missing or invalid: {categories!r}"]
    values, issues = [], []

    def add_location(value, field):
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{field} contains a missing or invalid location: {value!r}")
        elif value not in values:
            values.append(value)

    if "location" in categories:
        add_location(categories["location"], "categories.location")
    all_locations = categories.get("allLocations")
    if all_locations is not None:
        if not isinstance(all_locations, list):
            issues.append(f"categories.allLocations is not a list: {all_locations!r}")
        else:
            for location in all_locations:
                add_location(location, "categories.allLocations")
    if not values and not issues:
        issues.append("No location name was supplied")
    return values, issues


def bamboohr_raw_locations(job):
    """Build location strings from BambooHR's two structured address fields."""
    values, issues = [], []
    for field, names in (
        ("location", ("city", "state", "addressCountry")),
        ("atsLocation", ("city", "state", "country")),
    ):
        address = job.get(field)
        if address is None:
            continue
        if not isinstance(address, dict):
            issues.append(f"{field} is not an address object: {address!r}")
            continue
        parts = []
        valid = True
        for name in names:
            value = address.get(name)
            if value is None or value == "":
                continue
            if not isinstance(value, str) or not value.strip():
                issues.append(f"{field}.{name} contains an invalid value: {value!r}")
                valid = False
            else:
                parts.append(value)
        if parts and valid:
            text = ", ".join(parts)
            if text not in values:
                values.append(text)
    if not values and not issues:
        if str(job.get("locationType")) == "1":
            values.append("Remote")
        else:
            issues.append("No location address was supplied")
    return values, issues


def workday_location_count(summary):
    text = summary.get("locationsText")
    match = WORKDAY_LOCATION_COUNT.fullmatch(text) if isinstance(text, str) else None
    return int(match.group(1)) if match else None


def workday_raw_locations(info, summary, *, page_data=False):
    """Extract Workday location names or page JSON-LD addresses."""
    values, issues = [], []

    def add(value, field):
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{field} contains an invalid location: {value!r}")
        elif value not in values:
            values.append(value)

    supplied = 0
    if page_data:
        locations = info.get("jobLocation")
        if locations is not None:
            if not isinstance(locations, list):
                locations = [locations]
            for entry in locations:
                supplied += 1
                address = entry.get("address") if isinstance(entry, dict) else None
                if not isinstance(address, dict):
                    issues.append(f"jobLocation has an invalid address: {entry!r}")
                    continue
                parts = []
                for field in ("addressLocality", "addressRegion", "addressCountry"):
                    value = address.get(field)
                    if value is None or value == "":
                        continue
                    if not isinstance(value, str) or not value.strip():
                        issues.append(f"jobLocation.{field} contains an invalid value: {value!r}")
                        parts = []
                        break
                    parts.append(value)
                if parts:
                    add(", ".join(parts), "jobLocation")
                else:
                    issues.append(f"jobLocation has no usable address: {entry!r}")
    else:
        if "location" in info:
            supplied += 1
            add(info["location"], "location")
        additional = info.get("additionalLocations")
        if additional is not None:
            if not isinstance(additional, list):
                issues.append(f"additionalLocations is not a list: {additional!r}")
            else:
                if additional and "location" not in info:
                    issues.append("Primary location is missing")
                for value in additional:
                    supplied += 1
                    add(value, "additionalLocations")

    count = workday_location_count(summary)
    if count is not None and supplied < count:
        issues.append(f"Listing reports {count} locations but details supplied {supplied}")
    text = summary.get("locationsText")
    if not values and count is None and isinstance(text, str) and text.strip():
        add(text, "locationsText")
        issues.append("Detail has no usable location; using listing location")
    if not values and not issues:
        issues.append("No location was supplied")
    return values, issues


def icims_raw_locations(soup):
    """Extract locations and remote metadata from an iCIMS job detail page."""
    values, issues = [], []
    postings = []

    def collect_postings(value):
        if isinstance(value, list):
            for item in value:
                collect_postings(item)
        elif isinstance(value, dict):
            kind = value.get("@type")
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                postings.append(value)
            if "@graph" in value:
                collect_postings(value["@graph"])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            collect_postings(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, RecursionError) as error:
            issues.append(f"Invalid JSON-LD: {error}")

    remote = False
    for posting in postings:
        remote = remote or posting.get("jobLocationType") == "TELECOMMUTE"
        locations = posting.get("jobLocation")
        if locations is None:
            continue
        if not isinstance(locations, list):
            locations = [locations]
        for location in locations:
            if not isinstance(location, dict) or not isinstance(location.get("address"), dict):
                issues.append(f"jobLocation has an invalid address: {location!r}")
                continue
            address = location["address"]
            parts = []
            valid = True
            for field in ("addressLocality", "addressRegion", "addressCountry"):
                value = address.get(field)
                if value is None or value == "" or (isinstance(value, str) and value.strip().upper() == "UNAVAILABLE"):
                    continue
                if not isinstance(value, str) or not value.strip():
                    issues.append(f"jobLocation.{field} contains an invalid value: {value!r}")
                    valid = False
                else:
                    parts.append(value)
            if parts and valid:
                text = ", ".join(parts)
                if text not in values:
                    values.append(text)
            elif valid:
                issues.append(f"jobLocation has no usable address: {location!r}")

    if not values:
        for label in soup.select(".field-label"):
            if label.get_text(" ", strip=True).casefold() not in ("location", "job location", "job locations"):
                continue
            parent = label.parent
            if parent.name == "dt":
                element = parent.find_next_sibling("dd")
                text = element.get_text(" ", strip=True) if element else ""
            else:
                text = " ".join(
                    sibling.get_text(" ", strip=True)
                    for sibling in label.find_next_siblings()
                )
            if text and text not in values:
                values.append(text)
    if not values:
        if remote:
            values.append("Remote")
        elif not issues:
            issues.append("No location was supplied on the job page")
    return values, issues, "Remote" if remote else None


def store_locations(connection, ats_vendor, jobs, incomplete_urls=(), workplace_types=None):
    """Replace one vendor's job locations inside the caller's transaction.

    Initialize the location schema before opening the transaction. Each job is
    (posting URL, vendor job ID, list of raw location strings).
    """
    if not isinstance(ats_vendor, str) or not ats_vendor.strip():
        raise ValueError("An ATS vendor is required to store locations")
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS location_stage_jobs (url TEXT PRIMARY KEY, vendor_job_id TEXT NOT NULL, workplace_type TEXT)")
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS location_stage_raw (url TEXT NOT NULL, raw_text TEXT NOT NULL, PRIMARY KEY (url, raw_text))")
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS location_stage_incomplete (url TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM location_stage_jobs")
    connection.execute("DELETE FROM location_stage_raw")
    connection.execute("DELETE FROM location_stage_incomplete")
    workplace_types = workplace_types or {}
    vendor_ids = {}
    for url, vendor_id, _ in jobs:
        if url in vendor_ids and vendor_ids[url] != vendor_id:
            raise ValueError(
                f"Conflicting vendor job IDs for {url!r}: {vendor_ids[url]!r} and {vendor_id!r}"
            )
        vendor_ids[url] = vendor_id
    connection.executemany("INSERT INTO location_stage_jobs VALUES (?, ?, ?) ON CONFLICT(url) DO NOTHING",
                           ((url, vendor_id, workplace_types.get(url)) for url, vendor_id, _ in jobs))
    connection.executemany("INSERT INTO location_stage_incomplete VALUES (?) ON CONFLICT(url) DO NOTHING", ((url,) for url in incomplete_urls))
    connection.executemany(
        "INSERT INTO location_stage_raw VALUES (?, ?) ON CONFLICT(url, raw_text) DO NOTHING",
        ((url, raw) for url, _, locations in jobs for raw in locations),
    )
    missing = connection.execute("""
            SELECT s.url FROM location_stage_jobs s
            LEFT JOIN job_postings j ON j.job_url = s.url
            WHERE j.job_posting_id IS NULL LIMIT 1
    """).fetchone()
    if missing:
        raise ValueError(f"Cannot save locations: posting was not saved for {missing[0]!r}")
    connection.execute("""
            INSERT INTO raw_locations (ats_vendor, raw_text)
            SELECT DISTINCT ?, raw_text FROM location_stage_raw WHERE 1
            ON CONFLICT(ats_vendor, raw_text) DO NOTHING
    """, (ats_vendor,))
    connection.execute("""
            DELETE FROM job_raw_locations
            WHERE ats_vendor = ?
              AND job_posting_id IN (
                  SELECT j.job_posting_id FROM job_postings j
                  JOIN location_stage_jobs s ON s.url = j.job_url
                  WHERE NOT EXISTS (SELECT 1 FROM location_stage_incomplete i WHERE i.url = s.url)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM job_postings j
                  JOIN location_stage_raw s ON s.url = j.job_url
                  JOIN raw_locations r ON r.ats_vendor = ? AND r.raw_text = s.raw_text
                  WHERE j.job_posting_id = job_raw_locations.job_posting_id
                    AND r.raw_location_id = job_raw_locations.raw_location_id
              )
    """, (ats_vendor, ats_vendor))
    connection.execute("""
            INSERT INTO job_raw_locations (job_posting_id, ats_vendor, vendor_job_id, raw_location_id, workplace_type)
            SELECT j.job_posting_id, ?, sj.vendor_job_id, r.raw_location_id, sj.workplace_type
            FROM location_stage_raw s
            JOIN location_stage_jobs sj ON sj.url = s.url
            JOIN job_postings j ON j.job_url = s.url
            JOIN raw_locations r ON r.ats_vendor = ? AND r.raw_text = s.raw_text
            WHERE 1
            ON CONFLICT(job_posting_id, ats_vendor, raw_location_id)
            DO UPDATE SET vendor_job_id = excluded.vendor_job_id, workplace_type = excluded.workplace_type
    """, (ats_vendor, ats_vendor))
    connection.execute("""
            DELETE FROM locations
            WHERE raw_location_id IS NOT NULL
              AND raw_location_id IN (SELECT raw_location_id FROM raw_locations WHERE ats_vendor = ?)
              AND job_posting_id IN (
                  SELECT j.job_posting_id FROM job_postings j
                  JOIN location_stage_jobs s ON s.url = j.job_url
              )
              AND NOT EXISTS (
                  SELECT 1 FROM job_raw_locations l
                  WHERE l.job_posting_id = locations.job_posting_id
                    AND l.raw_location_id = locations.raw_location_id
              )
    """, (ats_vendor,))
    insert_clean_locations(connection, "jobs")


@dataclass(frozen=True)
class Place:
    city: str | None = None
    state: str | None = None
    country: str | None = None
    remote: bool = False
    hybrid: bool = False
    status: str = "resolved"
    reason: str | None = None


@lru_cache(maxsize=1)
def geographic_tables():
    import pycountry

    countries = {}
    for country in pycountry.countries:
        for name in (country.name, country.alpha_2, country.alpha_3,
                     getattr(country, "official_name", ""), getattr(country, "common_name", "")):
            if name:
                countries[key(name)] = country.alpha_2
    for name, code in {
        "usa": "US", "u.s.": "US", "u.s.a.": "US", "united states of america": "US",
        "uk": "GB", "u.k.": "GB", "great britain": "GB",
        "south korea": "KR", "north korea": "KP", "russia": "RU",
        "vietnam": "VN", "taiwan": "TW", "czech republic": "CZ",
    }.items():
        countries[key(name)] = code
    subdivisions = defaultdict(list)
    for subdivision in pycountry.subdivisions:
        country, code = subdivision.code.split("-", 1)
        if country not in ("US", "CA"):
            continue
        for name in (subdivision.name, code):
            subdivisions[(country, key(name))].append(code)
    subdivisions[("US", "washington dc")].append("DC")
    subdivisions[("US", "washington d.c.")].append("DC")
    return countries, subdivisions


@lru_cache(maxsize=1)
def load_gazetteer(path):
    """Read GeoNames' tab-separated cities file once per process."""
    cities = defaultdict(dict)
    with Path(path).open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 19:
                raise ValueError(f"Invalid GeoNames row {line_number} in {path}")
            try:
                population = int(fields[14])
            except ValueError as error:
                raise ValueError(f"Invalid population on GeoNames row {line_number}") from error
            admin = CANADA_ADMIN.get(fields[10], fields[10]) if fields[8] == "CA" else fields[10]
            record = (fields[0], fields[1], fields[8], admin, population)
            for name in (fields[1], fields[2], *fields[3].split(",")):
                if name:
                    cities[key(name)][fields[0]] = record
    if not cities:
        raise ValueError(f"GeoNames file is empty: {path}")
    return {name: tuple(records.values()) for name, records in cities.items()}


def parse_part(text, cities):
    countries, subdivisions = geographic_tables()
    remote = bool(REMOTE.search(text))
    hybrid = bool(HYBRID.search(text))
    text = PREFIX.sub("", text.strip()).strip(" -,:()")
    text = REMOTE_SUFFIX.sub("", text).strip()
    if not text or key(text) in ("us-remote", "usa-remote"):
        return Place(country="US" if text else None, remote=remote, hybrid=hybrid)

    match = HYPHEN_FORMAT.fullmatch(text)
    if match:
        parts = [match.group(3), match.group(2), match.group(1)]
    elif ">" in text:
        parts = [part.strip() for part in reversed(text.split(">"))]
    else:
        parts = [part.strip() for part in text.split(",")]
    parts = [ZIP.sub("", part).strip() for part in parts if part.strip()]
    if not parts:
        return Place(remote=remote, hybrid=hybrid, status="unresolved", reason="No location text")

    country = None
    last_is_state_code = (
        len(parts) == 2 and len(parts[-1]) == 2 and parts[-1].isupper()
        and any(subdivisions.get((code, key(parts[-1]))) for code in ("US", "CA"))
    )
    if not last_is_state_code and (len(parts) > 1 or len(parts[0]) > 2 or key(parts[0]) in ("us", "uk")):
        country = countries.get(key(parts[-1]))
        if country:
            parts.pop()

    state = None
    if parts:
        candidates = []
        for code in ((country,) if country in ("US", "CA") else ("US", "CA") if not country else ()):
            candidates.extend((code, state_code) for state_code in subdivisions.get((code, key(parts[-1])), ()))
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) == 1:
            country, state = candidates[0]
            parts.pop()
    if not parts:
        if country or state:
            return Place(state=state, country=country, remote=remote, hybrid=hybrid)
        return Place(remote=remote, hybrid=hybrid, status="unresolved", reason="No recognized location")

    if len(parts) > 1:
        return Place(state=state, country=country, remote=remote, hybrid=hybrid,
                     status="unresolved", reason="Unrecognized or multiple location components")

    city = parts[0]
    if STREET_ADDRESS.match(city):
        return Place(state=state, country=country, remote=remote, hybrid=hybrid,
                     status="unresolved", reason="Street address requires review")
    candidates = cities.get(key(city), ())
    if country:
        candidates = tuple(candidate for candidate in candidates if candidate[2] == country)
    if state:
        candidates = tuple(candidate for candidate in candidates if candidate[3] == state)
    if len(candidates) == 1:
        _, name, city_country, admin, _ = candidates[0]
        return Place(name, state or (admin if city_country in ("US", "CA") and admin else None),
                     country or city_country, remote, hybrid)
    if len(candidates) > 1:
        return Place(state=state, country=country, remote=remote, hybrid=hybrid,
                     status="unresolved", reason=f"Ambiguous city: {city}")
    if country and state:
        return Place(city, state, country, remote, hybrid)
    return Place(state=state, country=country, remote=remote, hybrid=hybrid,
                 status="unresolved", reason=f"City not found: {city}")


def parse_locations(raw, cities):
    return [parse_part(part, cities) for part in SPLIT.split(raw) if part.strip()] or [
        Place(status="unresolved", reason="Empty location")
    ]


def log_parse_error(connection, error, rows, ats_vendor):
    contexts = []
    try:
        for raw_id, raw in rows:
            links = connection.execute("""
                SELECT c.slug_name, j.company_id, j.job_posting_id,
                       l.vendor_job_id, j.job_url
                FROM job_raw_locations l
                JOIN job_postings j ON j.job_posting_id = l.job_posting_id
                LEFT JOIN companies c ON c.company_id = j.company_id
                WHERE l.raw_location_id = ?
            """, (raw_id,)).fetchall()
            if not links:
                contexts.append({"raw_location_id": raw_id, "location": raw})
            for company, company_id, posting_id, vendor_job_id, url in links:
                contexts.append({
                    "company": company, "company_id": company_id,
                    "job_posting_id": posting_id, "vendor_job_id": vendor_job_id,
                    "location": raw, "url": url,
                })
    except sqlite3.Error as context_error:
        contexts = [{"raw_location_id": raw_id, "location": raw} for raw_id, raw in rows]
        contexts.append({"context_lookup_error": str(context_error)})
    Error_Log_system().location_error(error, "Location parsing (candidate records)", ats_vendor, contexts)


def parse_database(connection, gazetteer, reparse=False, batch_size=1000, ats_vendor="Greenhouse"):
    initialize_location_schema(connection)
    cities = load_gazetteer(str(Path(gazetteer).resolve()))
    count = 0
    last_id = 0
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS location_parse_batch (raw_location_id INTEGER PRIMARY KEY)")
    while True:
        rows = connection.execute("""
            SELECT r.raw_location_id, r.raw_text FROM raw_locations r
            WHERE r.ats_vendor = ? AND r.raw_location_id > ?
              AND (? OR NOT EXISTS (SELECT 1 FROM parsed_locations p WHERE p.raw_location_id = r.raw_location_id))
            ORDER BY r.raw_location_id LIMIT ?
        """, (ats_vendor, last_id, reparse, batch_size)).fetchall()
        if not rows:
            return count
        results = []
        for raw_id, raw in rows:
            try:
                results.append((raw_id, parse_locations(raw, cities)))
            except Exception as error:
                log_parse_error(connection, error, [(raw_id, raw)], ats_vendor)
                raise
        try:
            with connection:
                connection.execute("DELETE FROM location_parse_batch")
                connection.executemany("INSERT INTO location_parse_batch VALUES (?)", ((raw_id,) for raw_id, _ in results))
                if reparse:
                    connection.execute("DELETE FROM locations WHERE raw_location_id IN (SELECT raw_location_id FROM location_parse_batch)")
                    connection.execute("DELETE FROM parsed_location_parts WHERE raw_location_id IN (SELECT raw_location_id FROM location_parse_batch)")
                    connection.execute("DELETE FROM parsed_locations WHERE raw_location_id IN (SELECT raw_location_id FROM location_parse_batch)")
                connection.executemany(
                    "INSERT INTO parsed_locations (raw_location_id) VALUES (?)",
                    ((raw_id,) for raw_id, _ in results),
                )
                connection.executemany("""
                    INSERT INTO parsed_location_parts
                    (raw_location_id, part_index, city_name, state_abbv, country, is_remote, is_hybrid, status, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, ((raw_id, index, place.city, place.state, place.country, place.remote,
                       place.hybrid, place.status, place.reason)
                      for raw_id, places in results for index, place in enumerate(places)))
                insert_clean_locations(connection, "raw")
        except Exception as error:
            log_parse_error(connection, error, rows, ats_vendor)
            raise
        count += len(rows)
        last_id = rows[-1][0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Parse stored ATS locations offline.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--gazetteer", type=Path, default=DEFAULT_GAZETTEER,
                        help="Path to GeoNames cities15000.txt or cities5000.txt")
    parser.add_argument("--vendor", choices=("all", "Greenhouse", "Ashby", "Lever", "BambooHR", "iCIMS", "Workday"), default="all",
                        help="Select a vendor to parse (default: all supported vendors)")
    parser.add_argument("--reparse", action="store_true", help="Reparse all strings for the selected vendors")
    args = parser.parse_args(argv)
    try:
        if not args.db.is_file():
            raise FileNotFoundError(f"Database does not exist: {args.db}")
        if not args.gazetteer.is_file():
            raise FileNotFoundError(f"GeoNames file does not exist: {args.gazetteer}")
        with sqlite3.connect(args.db, timeout=120) as connection:
            vendors = ("Greenhouse", "Ashby", "Lever", "BambooHR", "iCIMS", "Workday") if args.vendor == "all" else (args.vendor,)
            for vendor in vendors:
                count = parse_database(connection, args.gazetteer, args.reparse, ats_vendor=vendor)
                print(f"Parsed {count} distinct {vendor} location strings.")
    except Exception as error:
        Error_Log_system().location_error(
            error, "Location parser command", args.vendor,
            [{"database": str(args.db), "gazetteer": str(args.gazetteer)}],
        )
        print(f"Location parsing failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
