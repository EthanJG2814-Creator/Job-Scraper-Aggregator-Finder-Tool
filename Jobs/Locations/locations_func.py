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
_GAZETTEERS = [Path(__file__).with_name(name) for name in ("cities5000.txt", "cities15000.txt")]
DEFAULT_GAZETTEER = next((path for path in _GAZETTEERS if path.is_file()), _GAZETTEERS[-1])
# Bump when parsing rules change so the next run reparses every stored string.
PARSER_VERSION = 3
SPLIT = re.compile(r"\s*[;|]\s*")
REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
PREFIX = re.compile(r"^(?:100%\s*)?(?:remote|hybrid)(?:\s+(?:or|in|from)\b)?\s*(?:[-,:\u2013\u2014]\s*)?", re.IGNORECASE)
ANYWHERE = re.compile(r"^(?:anywhere|any\s+location)(?:\s+in)?(?:\s+the)?\s*[,:-]?\s*", re.IGNORECASE)
HOME_BASED = re.compile(r"^home[\s-]*based\b", re.IGNORECASE)
ZIP = re.compile(r"\s+\d{5}(?:-\d{4})?$")
HYPHEN_FORMAT = re.compile(r"^(US|USA)-([A-Za-z]{2})-(.+)$", re.IGNORECASE)
WORKDAY_LOCATION_COUNT = re.compile(r"^\s*(\d+)\s+locations?\s*$", re.IGNORECASE)
REMOTE_SUFFIX = re.compile(r"(?:,\s*|\s*[-/\u2013\u2014]\s*|\s+(?:or\s+)?)(?:remote|hybrid)$", re.IGNORECASE)
STREET_ADDRESS = re.compile(r"^\d+\s+\S")
COORDINATES = re.compile(r"\s*\|\s*-?\d+(?:\.\d+)?\s*\|\s*-?\d+(?:\.\d+)?")
FLAG_PAREN = re.compile(r"\s*\((?=[^)]*\b(?:remote|hybrid|hq|headquarters)\b)[^)]*\)", re.IGNORECASE)
PAREN = re.compile(r"\s*\(([^)]*)\)")
CODE = re.compile(r"[A-Z]{2,3}")
OFFICE_SUFFIX = re.compile(
    r"(?:\s+|\s*-\s*)(?:(?:head\s*)?office|hq|headquarters|hub|campus|(?:(?:bay|metropolitan|metropolitain|metro)\s+)?area)$",
    re.IGNORECASE,
)
ADMIN_SUFFIX = re.compile(r"\s+(?:city|prefecture|district|province|sar)$", re.IGNORECASE)
COUNTRY_SUFFIX = re.compile(r"^([A-Za-z]{2})\s+([A-Za-z. ]{2,})$")
REMOTE_ONLY = {"anywhere", "distributed", "remoto", "work from home", "wfh", "home based", "world wide", "worldwide"}
SPACED_DASH = re.compile(r"\s+[-/\u2013\u2014]\s+")
STATE_DASH = re.compile(r"^([A-Z]{2})-(.+)$")
CITY_STATE = re.compile(r"^(.+?)\s+([A-Z][A-Za-z])$")
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
    if not values:
        office_values = greenhouse_office_locations(job)
        if office_values:
            return office_values, []
    if not values and not issues:
        issues.append("No location name was supplied")
    return values, issues


def greenhouse_office_locations(job):
    """Fallback: location strings from a job's offices, skipping parent offices
    whose children are also listed (e.g. a region above a city)."""
    offices = [o for o in job.get("offices") or [] if isinstance(o, dict)]
    listed_ids = {o.get("id") for o in offices}
    values = []
    for office in offices:
        children = office.get("child_ids") or []
        if any(child in listed_ids for child in children):
            continue
        for key in ("location", "name"):
            value = office.get(key)
            if isinstance(value, str) and value.strip():
                if value not in values:
                    values.append(value)
                break
    return values


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
    if not values:
        office_location = ashby_office_location(job)
        if office_location:
            return [office_location], []
    return values, issues


def ashby_office_location(job):
    """Fallback: build a location string from the job's address.postalAddress."""
    address = job.get("address")
    postal = address.get("postalAddress") if isinstance(address, dict) else None
    if not isinstance(postal, dict):
        return None
    parts = [
        postal[key].strip()
        for key in ("addressLocality", "addressRegion", "addressCountry")
        if isinstance(postal.get(key), str) and postal[key].strip()
    ]
    return ", ".join(parts) or None


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
        "turkey": "TR", "korea": "KR", "brasil": "BR", "deutschland": "DE", "uae": "AE", "holland": "NL",
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
    subdivisions[("US", "d.c.")].append("DC")
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


def choose_city(candidates, name):
    """Pick one gazetteer record for a city name, or None if it is truly ambiguous."""
    # Alternate names also match unrelated places ("Newton" for "Cambridge"),
    # so prefer places whose own name matches.
    candidates = tuple(candidate for candidate in candidates if key(candidate[1]) == key(name)) or candidates
    ranked = sorted(candidates, key=lambda candidate: candidate[4], reverse=True)
    if not ranked:
        return None
    # Many big cities share a name with small towns, so accept a candidate
    # that is at least twice as populous as the next one.
    if len(ranked) == 1 or ranked[0][4] >= 2 * ranked[1][4]:
        return ranked[0]
    # Postings skew toward the US, so a lone US city close in size to the
    # biggest namesake wins ("Santa Clara", "Cambridge").
    us_cities = [candidate for candidate in ranked if candidate[2] == "US" and candidate[4] >= 0.4 * ranked[0][4]]
    return us_cities[0] if len(us_cities) == 1 else None


def unwrap_paren(match):
    """Keep parenthesized places ("HQ (Austin, TX)", "Berlin (DE)"); drop other notes."""
    inner = match.group(1).strip()
    return f", {inner}" if "," in inner or CODE.fullmatch(inner) else ""


def parse_clean_part(text, cities):
    countries, subdivisions = geographic_tables()
    remote = bool(REMOTE.search(text))
    hybrid = bool(HYBRID.search(text))
    text = FLAG_PAREN.sub("", text.strip())
    text = PAREN.sub(unwrap_paren, PREFIX.sub("", text.strip())).strip(" -,:()*")
    text = REMOTE_SUFFIX.sub("", text).strip().replace("_", "-")
    text = OFFICE_SUFFIX.sub("", text).strip(" -,:()*")
    if HOME_BASED.match(text):
        return Place(remote=True, hybrid=hybrid)
    if ANYWHERE.match(text):
        remote = True
        text = ANYWHERE.sub("", text).strip(" -,:()*")
    if not text or key(text) in ("us-remote", "usa-remote"):
        return Place(country="US" if text else None, remote=remote, hybrid=hybrid)
    if key(text) in REMOTE_ONLY:
        return Place(remote=True, hybrid=hybrid)

    match = HYPHEN_FORMAT.fullmatch(text)
    if match:
        parts = [match.group(3), match.group(2), match.group(1)]
    elif ">" in text:
        parts = [part.strip() for part in reversed(text.split(">"))]
    else:
        pieces = SPACED_DASH.split(text)

        def is_region(piece):
            name = key(piece)
            return bool(countries.get(name) or subdivisions.get(("US", name)) or subdivisions.get(("CA", name)))

        if len(pieces) > 1 and "," not in pieces[-1] and is_region(pieces[0]) and not is_region(pieces[-1]):
            pieces.reverse()
        parts = [part.strip() for piece in pieces for part in piece.split(",")]
    parts = [ZIP.sub("", part).strip() for part in parts if part.strip()]
    parts = [part for index, part in enumerate(parts) if index == 0 or key(part) != key(parts[index - 1])]
    if not parts:
        return Place(remote=remote, hybrid=hybrid, status="unresolved", reason="No location text")
    # A country glued to the last part, such as "CA United States".
    match = COUNTRY_SUFFIX.fullmatch(parts[-1])
    if match and len(parts) > 1 and countries.get(key(match.group(2))) \
            and any(subdivisions.get((region, key(match.group(1)))) for region in ("US", "CA")):
        parts[-1:] = match.groups()
    if len(parts) == 1:
        # "City ST" and "ST-City" carry a US/Canada state code without a comma.
        match = STATE_DASH.fullmatch(parts[0])
        if match:
            code, city = match.groups()
        elif match := CITY_STATE.fullmatch(parts[0]):
            city, code = match.groups()
        else:
            city = code = None
        if code and any(subdivisions.get((region, key(code))) for region in ("US", "CA")):
            parts = [city.strip(" ,"), code]

    def is_state_code(part):
        return len(part) == 2 and part[0].isupper() and any(
            subdivisions.get((region, key(part))) for region in ("US", "CA")
        )

    country = None
    country_code = False
    # A trailing code is a state unless it follows another state ("Toronto, ON, CA").
    last_is_state_code = len(parts) > 1 and is_state_code(parts[-1]) and (
        len(parts) == 2 or not is_state_code(parts[-2])
    )
    if not last_is_state_code and (len(parts) > 1 or len(parts[0]) > 2 or key(parts[0]) in ("us", "uk")):
        country = countries.get(key(parts[-1]))
        # "Atlanta, Georgia" names a US state even though Georgia is also a country.
        state_codes = subdivisions.get(("US", key(parts[-1])), ()) if len(parts) > 1 else ()
        if country and state_codes and any(
            candidate[2] == "US" and candidate[3] in state_codes for candidate in cities.get(key(parts[-2]), ())
        ):
            country = None
        if country:
            country_code = len(parts[-1]) == 2
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

    def lookup(city, strict=False):
        """Resolve a city name against the gazetteer using any known state/country."""
        if STREET_ADDRESS.match(city):
            return Place(state=state, country=country, remote=remote, hybrid=hybrid,
                         status="unresolved", reason="Street address requires review")
        candidates = cities.get(key(city), ())
        if country:
            candidates = tuple(candidate for candidate in candidates if candidate[2] == country)
        if state:
            candidates = tuple(candidate for candidate in candidates if candidate[3] == state)
        chosen = choose_city(candidates, city)
        if chosen:
            _, name, city_country, admin, _ = chosen
            return Place(name, state or (admin if city_country in ("US", "CA") and admin else None),
                         country or city_country, remote, hybrid)
        if candidates:
            return Place(state=state, country=country, remote=remote, hybrid=hybrid,
                         status="unresolved", reason=f"Ambiguous city: {city}")
        if ADMIN_SUFFIX.search(city) and not state:
            return lookup(ADMIN_SUFFIX.sub("", city), strict)
        if country and state and not strict:
            return Place(city, state, country, remote, hybrid)
        return Place(state=state, country=country, remote=remote, hybrid=hybrid,
                     status="unresolved", reason=f"City not found: {city}")

    if len(parts) > 1:
        # Extra components are usually districts or regions around the city, so
        # accept only a gazetteer-backed match on the first or last component.
        for city in (parts[0], parts[-1]):
            place = lookup(city, strict=True)
            if place.status == "resolved":
                return place
        if country and state:
            # Small towns missing from the gazetteer: "Business Name - Town, ST".
            place = lookup(parts[-1])
            if place.status == "resolved":
                return place
        return Place(state=state, country=country, remote=remote, hybrid=hybrid,
                     status="unresolved", reason="Unrecognized or multiple location components")
    place = lookup(parts[0])
    if place.status != "resolved" and country_code and not state:
        # "Belo Horizonte, MG": a region code that only looks like a country code.
        country = None
        retry = lookup(parts[0], strict=True)
        if retry.status == "resolved":
            return retry
    return place


STREET_WORDS = {
    "st", "street", "ave", "avenue", "rd", "road", "blvd", "boulevard", "dr", "drive", "way", "ln", "lane",
    "pkwy", "parkway", "hwy", "highway", "suite", "ste", "fl", "floor", "bldg", "building", "ct", "court",
    "pl", "place", "plaza", "sq", "square", "route", "rte", "center", "centre", "park", "tower", "house",
    "office", "hq", "avenida", "calle", "rue",
}
SHORT_COUNTRY_NAMES = {"US": "US", "USA": "US", "UK": "GB", "UAE": "AE", "U.S.": "US", "U.S.A.": "US", "U.K.": "GB"}
NOT_COUNTRY_NAMES = {"chad", "jersey", "niger", "man", "georgia"}
UK_PLACES = {
    "england", "scotland", "wales", "northern ireland", "west midlands", "east midlands", "south yorkshire",
    "west yorkshire", "tyne and wear", "merseyside", "greater manchester", "greater london", "cornwall",
    "devon", "dorset", "cumbria", "rutland", "isle of wight", "east sussex", "west sussex",
}
COUNTRY_ALIASES = {
    "maroc": "MA", "espana": "ES", "italia": "IT", "nederland": "NL", "schweiz": "CH", "osterreich": "AT",
    "new south wales": "AU", "new england": "US",
}
PARTIAL_SEGMENT = re.compile(r"[,;|()\[\]/]|\s[-\u2013\u2014]\s")
ZIP_WORD = re.compile(r"^\d{5}(?:-\d{4})?$")


@lru_cache(maxsize=1)
def partial_tables():
    countries, subdivisions = geographic_tables()
    states = {}
    for (region, name), codes in subdivisions.items():
        if len(name) > 2 and len(set(codes)) == 1:
            states[name] = (region, codes[0])
    names = {name: code for name, code in countries.items() if len(name) > 3 and name not in NOT_COUNTRY_NAMES}
    names.update({key(name): "GB" for name in UK_PLACES})
    names.update(COUNTRY_ALIASES)
    return states, names


def partial_place(text, cities, base):
    """Salvage the state or country (and a city if one is found) from text that did not parse cleanly."""
    countries, subdivisions = geographic_tables()
    state_names, country_names = partial_tables()
    segments = [segment.split() for segment in PARTIAL_SEGMENT.split(text)]
    states, found_countries, consumed, spans = [], [], set(), []
    for s, words in enumerate(segments):
        i = 0
        while i < len(words):
            matched = False
            for size in range(min(4, len(words) - i), 0, -1):
                phrase = " ".join(words[i:i + size]).strip(",;:*")
                name = key(phrase)
                following = key(words[i + size]).rstrip(".") if i + size < len(words) else ""
                if size == 1 and following in STREET_WORDS and name in state_names:
                    continue
                if name in state_names:
                    # State words stay available so a longer city name can still contain them ("New York City").
                    states.append((*state_names[name], "ambiguous" if name == "georgia" else "name"))
                    spans.append((s, i, size))
                    i += size
                    matched = True
                    break
                if phrase in SHORT_COUNTRY_NAMES:
                    found_countries.append(SHORT_COUNTRY_NAMES[phrase])
                elif name in country_names and not (size == 1 and following in STREET_WORDS):
                    found_countries.append(country_names[name])
                elif size == 1 and len(words) == 1 and len(name) > 6 and name.endswith("shire"):
                    found_countries.append("GB")
                else:
                    continue
                consumed.update((s, i + j) for j in range(size))
                i += size
                matched = True
                break
            if matched:
                continue
            word = words[i].strip(".,;:")
            if word.isupper() and len(word) == 2 and (i == 0 and s > 0 or i + 1 < len(words) and ZIP_WORD.match(words[i + 1])):
                for region in ("US", "CA"):
                    codes = subdivisions.get((region, key(word)))
                    if codes and len(set(codes)) == 1:
                        states.append((region, codes[0], "code"))
                        consumed.add((s, i))
                        break
            i += 1

    us_hint = any(country == "US" for country in found_countries) or any(
        ZIP_WORD.match(word) for words in segments for word in words
    )
    all_words = [word.strip(",;:*") for words in segments for word in words]
    grams = {key(" ".join(all_words[i:i + size])) for i in range(len(all_words)) for size in (1, 2, 3)}
    if any(state[2] == "ambiguous" for state in states):
        # "Georgia" is a US state unless the text names a city that only exists in the country.
        in_country = any(c[2] == "GE" for gram in grams for c in cities.get(gram, ()))
        in_state = any(c[2] == "US" and c[3] == "GA" for gram in grams for c in cities.get(gram, ()))
        if in_country and not in_state and not us_hint:
            states = [state for state in states if state[2] != "ambiguous"]
            found_countries.append("GE")
    country = state = None
    from_code = False
    if states:
        distinct = list(dict.fromkeys((region, code) for region, code, _ in states))
        codes = list(dict.fromkeys((region, code) for region, code, kind in states if kind == "code"))
        # "Washington, DC 20001" mentions two states; an abbreviation beats a name.
        pick = distinct[0] if len(distinct) == 1 else codes[0] if len(codes) == 1 else None
        if pick:
            country, state = pick
            from_code = pick in codes
        elif len({region for region, _ in distinct}) == 1:
            country = distinct[0][0]
        if found_countries and country not in found_countries:
            country = state = None
    if not country:
        distinct = list(dict.fromkeys(found_countries))
        if len(distinct) != 1:
            return None
        country = distinct[0]

    def in_state_span(s, i, size):
        for span_s, span_i, span_size in spans:
            if span_s == s and i < span_i + span_size and span_i < i + size:
                # With an explicit abbreviation ("New York, NY") the state-named words can be the city.
                if from_code and i == span_i and size == span_size:
                    continue
                if not (i <= span_i and i + size >= span_i + span_size and size > span_size):
                    return True
        return False

    def usable(s, i, size):
        """A word run that could be a city name rather than a street, number or already-used word."""
        if any((s, i + j) in consumed for j in range(size)) or in_state_span(s, i, size):
            return None
        words = segments[s][i:i + size]
        phrase = " ".join(words).strip(",;:*")
        parts = [key(word).rstrip(".") for word in words]
        following = key(segments[s][i + size]).rstrip(".") if i + size < len(segments[s]) else ""
        if any(char.isdigit() for char in phrase) or all(part in STREET_WORDS for part in parts) \
                or following in STREET_WORDS or parts[0] in STREET_WORDS or (key(phrase) in state_names and not from_code):
            return None
        return phrase

    # A lone US or Canada country match gives too little context to trust a city name.
    city = None
    if state or country not in ("US", "CA"):
        for s, words in enumerate(segments):
            i = 0
            while i < len(words):
                for size in range(min(4, len(words) - i), 0, -1):
                    phrase = usable(s, i, size)
                    if not phrase:
                        continue
                    candidates = tuple(
                        candidate for candidate in cities.get(key(phrase), ())
                        if candidate[2] == country and (not state or candidate[3] == state)
                    )
                    chosen = choose_city(candidates, phrase)
                    if chosen:
                        city = chosen
                        i += size - 1
                        break
                i += 1
    if not state and not city:
        # "London/New York/Singapore" names several places; a single country would misfile it.
        for s, words in enumerate(segments):
            for i in range(len(words)):
                for size in (1, 2, 3):
                    phrase = usable(s, i, size) if i + size <= len(words) else None
                    other = choose_city(cities.get(key(phrase), ()), phrase) if phrase and len(phrase) > 3 else None
                    if other and other[2] != country and other[4] >= 100000:
                        return None
    found = [label for label, value in (("country", country), ("state", state), ("city", city)) if value]
    name, admin = (city[1], city[3]) if city else (None, None)
    return Place(name, state or (admin if country in ("US", "CA") else None), country, base.remote, base.hybrid,
                 reason=f"Partial match ({', '.join(found)}); original: {base.reason}")


def parse_part(text, cities):
    place = parse_clean_part(text, cities)
    if place.status == "resolved":
        return place
    return partial_place(text, cities, place) or place


def parse_locations(raw, cities):
    return [parse_part(part, cities) for part in SPLIT.split(COORDINATES.sub("", raw)) if part.strip()] or [
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
            stale = args.vendor == "all" and connection.execute("PRAGMA user_version").fetchone()[0] != PARSER_VERSION
            if stale:
                print("Parser rules changed since the last run; reparsing all stored locations.")
            for vendor in vendors:
                count = parse_database(connection, args.gazetteer, args.reparse or stale, ats_vendor=vendor)
                print(f"Parsed {count} distinct {vendor} location strings.")
            if args.vendor == "all":
                connection.execute(f"PRAGMA user_version = {PARSER_VERSION}")
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
# job function exectuion
# python -m Jobs.Locations.locations_func