import sqlite3
import os
import tempfile
import unittest
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from Jobs.Locations.locations_func import (
    greenhouse_raw_locations,
    initialize_location_schema,
    load_gazetteer,
    parse_database,
    parse_locations,
    store_locations,
)
from Jobs.greenhouse_main import greenhouse_main_system
from The_Database.db_connect import database_connection_system
from logging_errors import Error_Log_system


def geonames_row(identifier, name, country, admin, population, alternates=""):
    fields = [identifier, name, name, alternates, "0", "0", "P", "PPL",
              country, "", admin, "", "", "", str(population), "", "", "UTC", "2024-01-01"]
    return "\t".join(fields)


class GreenhouseLocationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.gazetteer = Path(self.directory.name) / "cities15000.txt"
        self.gazetteer.write_text("\n".join([
            geonames_row("1", "Bangkok", "TH", "40", 10000000),
            geonames_row("2", "Vancouver", "CA", "02", 600000),
            geonames_row("3", "Vancouver", "US", "WA", 200000),
            geonames_row("4", "Gothenburg", "SE", "28", 500000, "Goteborg"),
            geonames_row("5", "Pune", "IN", "16", 3000000),
            geonames_row("6", "Tokyo", "JP", "40", 9000000, "東京"),
        ]) + "\n", encoding="utf-8")
        self.cities = load_gazetteer(str(self.gazetteer))
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE job_postings (job_posting_id INTEGER PRIMARY KEY, job_url TEXT UNIQUE);
            INSERT INTO job_postings VALUES (7, 'https://example.test/7'), (8, 'https://example.test/8');
        """)

    def store_locations(self, jobs, vendor="Greenhouse", connection=None):
        connection = connection or self.connection
        initialize_location_schema(connection)
        with connection:
            store_locations(connection, vendor, jobs)

    def test_rules_and_gazetteer(self):
        def part(text):
            return parse_locations(text, self.cities)[0]

        self.assertEqual((part("US > Oregon > Hillsboro").city, part("US > Oregon > Hillsboro").state,
                          part("US > Oregon > Hillsboro").country), ("Hillsboro", "OR", "US"))
        self.assertEqual((part("US-VA-Arlington").city, part("US-VA-Arlington").state), ("Arlington", "VA"))
        self.assertEqual((part("Marietta, GA").city, part("Marietta, GA").country), ("Marietta", "US"))
        self.assertEqual((part("Quebec, Canada").state, part("Quebec, Canada").country), ("QC", "CA"))
        self.assertEqual((part("Remote - Philippines").country, part("Remote - Philippines").remote), ("PH", True))
        self.assertEqual((part("US-Remote").country, part("US-Remote").remote), ("US", True))
        self.assertEqual((part("United States, Remote").country, part("United States, Remote").remote), ("US", True))
        self.assertTrue(part("Hybrid, Pune").hybrid)
        self.assertEqual(part("Bangkok").country, "TH")
        self.assertEqual(part("Goteborg").city, "Gothenburg")
        self.assertEqual(part("東京").city, "Tokyo")
        self.assertEqual(part("大阪").status, "unresolved")
        self.assertEqual(part("Vancouver, BC").country, "CA")
        self.assertEqual(part("Vancouver, BC").state, "BC")
        self.assertEqual(part("Vancouver").status, "unresolved")
        self.assertIn("Ambiguous", part("Vancouver").reason)
        self.assertEqual(part("Lake Erie Preparatory School").status, "unresolved")
        self.assertEqual(part("5205 South West 91st Dr., Gainesville, FL 32608").status, "unresolved")
        self.assertEqual(part("123 Main St, FL").status, "unresolved")
        self.assertEqual(part("100% Remote (Europe*)").status, "unresolved")

    def test_splitting_and_raw_extraction(self):
        parts = parse_locations("US-Remote | US-VA-Arlington; Marietta, GA", self.cities)
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].remote)
        self.assertEqual([p.city for p in parts[1:]], ["Arlington", "Marietta"])
        self.assertEqual(greenhouse_raw_locations({
            "location": {"name": "  Bangkok  "},
            "locations": [{"name": "  Bangkok  "}, {"name": "Pune"}],
        }), (["  Bangkok  ", "Pune"], []))
        for location in ({"name": None}, {"name": 42}, {"name": ""}, {}):
            names, issues = greenhouse_raw_locations({"id": 5, "location": location})
            self.assertEqual(names, [])
            self.assertTrue(issues)

    def test_posting_links_distinct_parsing_and_repeat_scrapes(self):
        first = [
            ("https://example.test/7", "700", ["Bangkok", "US-VA-Arlington; Marietta, GA"]),
            ("https://example.test/8", "800", ["Bangkok"]),
        ]
        self.store_locations(first)
        with patch("Jobs.Locations.locations_func.parse_locations", wraps=parse_locations) as parse:
            self.assertEqual(parse_database(self.connection, self.gazetteer), 2)
            self.assertEqual(parse.call_count, 2)
            self.assertEqual(parse_database(self.connection, self.gazetteer), 0)
            self.assertEqual(parse.call_count, 2)
        rows = self.connection.execute("""
            SELECT job_posting_id, vendor_job_id, raw_text, city_name, country, status
            FROM job_parsed_locations ORDER BY job_posting_id, raw_text, part_index
        """).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertIn((7, "700", "Bangkok", "Bangkok", "TH", "resolved"), rows)
        self.assertIn((8, "800", "Bangkok", "Bangkok", "TH", "resolved"), rows)
        clean = self.connection.execute("""
            SELECT job_posting_id, city_name, state_abbv, country FROM locations
            ORDER BY job_posting_id, city_name
        """).fetchall()
        self.assertEqual(clean, [(7, "Arlington", "VA", "US"), (7, "Bangkok", None, "TH"),
                                 (7, "Marietta", "GA", "US"), (8, "Bangkok", None, "TH")])
        self.store_locations(first)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM job_raw_locations").fetchone()[0], 3)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 4)
        self.store_locations([
            ("https://example.test/7", "701", ["Pune"]),
            ("https://example.test/8", "800", []),
        ])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM job_raw_locations").fetchone()[0], 1)
        self.assertEqual(parse_database(self.connection, self.gazetteer), 1)
        self.assertEqual(self.connection.execute(
            "SELECT job_posting_id, vendor_job_id, city_name FROM job_parsed_locations"
        ).fetchone(), (7, "701", "Pune"))
        self.assertEqual(self.connection.execute(
            "SELECT job_posting_id, city_name, country FROM locations"
        ).fetchall(), [(7, "Pune", "IN")])
        self.assertEqual(parse_database(self.connection, self.gazetteer, reparse=True), 3)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 1)
        self.store_locations([("https://example.test/8", "800", ["Pune", "Vancouver"])])
        self.assertEqual(parse_database(self.connection, self.gazetteer), 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM job_parsed_locations WHERE raw_text = 'Vancouver'"
        ).fetchone()[0], "unresolved")

    def test_duplicate_urls_merge_locations_without_duplicate_links(self):
        self.store_locations([
            ("https://example.test/7", "700", ["Bangkok", "Pune"]),
            ("https://example.test/7", "700", ["Bangkok", "Vancouver"]),
        ])
        self.assertEqual(self.connection.execute(
            "SELECT raw_text FROM job_parsed_locations ORDER BY raw_text"
        ).fetchall(), [("Bangkok",), ("Pune",), ("Vancouver",)])
        self.assertEqual(parse_database(self.connection, self.gazetteer), 3)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)

    def test_conflicting_ids_rollback_without_changing_existing_links(self):
        self.store_locations([("https://example.test/7", "700", ["Bangkok"])])
        with self.assertRaisesRegex(ValueError, "Conflicting vendor job IDs"):
            self.store_locations([
                ("https://example.test/7", "700", ["Pune"]),
                ("https://example.test/7", "701", ["Vancouver"]),
            ])
        self.assertEqual(self.connection.execute(
            "SELECT vendor_job_id, raw_text FROM job_parsed_locations"
        ).fetchall(), [("700", "Bangkok")])

    def test_missing_posting_does_not_change_existing_links(self):
        self.store_locations([("https://example.test/7", "700", ["Bangkok"])])
        with self.assertRaises(ValueError):
            self.store_locations([
                ("https://example.test/missing", "900", ["Pune"]),
            ])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM job_raw_locations").fetchone()[0], 1)

    def test_vendor_storage_is_isolated_for_the_same_posting(self):
        self.store_locations([("https://example.test/7", "700", ["Bangkok"])])
        self.store_locations([("https://example.test/7", "other-7", ["Pune"])], vendor="OtherATS")
        self.assertEqual(self.connection.execute("""
            SELECT ats_vendor, vendor_job_id FROM job_raw_locations ORDER BY ats_vendor
        """).fetchall(), [("Greenhouse", "700"), ("OtherATS", "other-7")])
        self.assertEqual(parse_database(self.connection, self.gazetteer), 1)
        other_id = self.connection.execute(
            "SELECT raw_location_id FROM raw_locations WHERE ats_vendor = 'OtherATS'"
        ).fetchone()[0]
        self.connection.execute("INSERT INTO parsed_locations (raw_location_id) VALUES (?)", (other_id,))
        self.connection.execute("""
            INSERT INTO parsed_location_parts
            (raw_location_id, part_index, city_name, country, is_remote, is_hybrid, status)
            VALUES (?, 0, 'Pune', 'IN', 0, 0, 'resolved')
        """, (other_id,))
        self.connection.commit()
        self.store_locations([("https://example.test/7", "other-7", ["Pune"])], vendor="OtherATS")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)
        self.store_locations([("https://example.test/7", "other-7", [])], vendor="OtherATS")
        self.assertEqual(self.connection.execute("""
            SELECT ats_vendor FROM job_raw_locations
        """).fetchall(), [("Greenhouse",)])
        self.assertEqual(self.connection.execute("""
            SELECT city_name FROM locations
        """).fetchall(), [("Bangkok",)])
        self.store_locations([("https://example.test/7", "other-7", ["Pune"])], vendor="OtherATS")
        self.store_locations([("https://example.test/7", "700", [])])
        self.assertEqual(self.connection.execute("""
            SELECT ats_vendor FROM job_raw_locations
        """).fetchall(), [("OtherATS",)])
        self.assertEqual(self.connection.execute("SELECT city_name FROM locations").fetchall(), [("Pune",)])
        with self.assertRaises(ValueError):
            self.store_locations([], vendor="")

    def test_existing_locations_table_is_migrated_without_losing_rows(self):
        self.connection.executescript("""
            CREATE TABLE locations (
                location_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_posting_id INTEGER NOT NULL,
                city_name TEXT, state_abbv TEXT, country TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO locations(job_posting_id, city_name, country) VALUES (7, 'Existing', 'US');
        """)
        self.store_locations([("https://example.test/7", "700", ["Bangkok"])])
        parse_database(self.connection, self.gazetteer)
        self.assertEqual(self.connection.execute(
            "SELECT city_name, raw_location_id FROM locations ORDER BY location_id"
        ).fetchall(), [("Existing", None), ("Bangkok", 1)])
        parse_database(self.connection, self.gazetteer, reparse=True)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)

    def test_invalid_gazetteer_is_not_silently_accepted(self):
        self.gazetteer.write_text("invalid\n", encoding="utf-8")
        load_gazetteer.cache_clear()
        with self.assertRaisesRegex(ValueError, "Invalid GeoNames row 1"):
            parse_database(self.connection, self.gazetteer)

    def test_scraper_saves_postings_and_locations_atomically(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("""
            CREATE TABLE job_postings (
                job_posting_id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL,
                job_title TEXT NOT NULL,
                job_remote TEXT,
                job_description TEXT NOT NULL,
                published_date TEXT,
                employment_type TEXT,
                job_url TEXT UNIQUE,
                compensation_summary TEXT,
                last_scanned TEXT
            )
        """)

        class FakeDatabase(database_connection_system):
            def __init__(self):
                self.db_connection = connection
                self.log_errors = Error_Log_system()

            def get_company_slug_map(self):
                return {"board": 4}

            def bulk_insert_scrape_logs(self, rows):
                self.logs = rows

        scraper = greenhouse_main_system.__new__(greenhouse_main_system)
        scraper.database = FakeDatabase()
        job = {
            "id": 123, "absolute_url": "https://example.test/123",
            "title": "Engineer", "content": "<p>Example</p>",
            "location": {"name": "Bangkok"},
            "locations": [{"name": "Pune"}],
        }
        scraper.slug_processing = lambda slug: {
            "company_slug": slug, "ats_vendor": "Greenhouse", "status": "success",
            "job_count": 1, "error": None, "jobs": [job],
        }
        scraper.data_aquasition(["board"])
        rows = connection.execute("""
            SELECT job_posting_id, vendor_job_id, raw_text
            FROM job_parsed_locations ORDER BY raw_text
        """).fetchall()
        self.assertEqual(rows, [(1, "123", "Bangkok"), (1, "123", "Pune")])
        scraper.data_aquasition(["board"])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_raw_locations").fetchone()[0], 2)

        inserted_job = (4, "Other", "N/A", "Description", None, "N/A",
                        "https://example.test/new", "N/A", "2024-01-01")
        log_path = Path(self.directory.name) / "errors.txt"
        def open_log(_path, *args, **kwargs):
            return open(log_path, *args, **kwargs)
        with patch("logging_errors.open", side_effect=open_log):
            with self.assertRaises(ValueError):
                scraper.database.bulk_insert_jobs_with_locations([inserted_job], [
                    ("https://example.test/missing", "999", ["Bangkok"]),
                ], "Greenhouse")
        self.assertIsNone(connection.execute(
            "SELECT job_posting_id FROM job_postings WHERE job_url = ?",
            ("https://example.test/new",),
        ).fetchone())

    def test_scraper_missing_location_does_not_abort_or_erase_existing_locations(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("""
            CREATE TABLE job_postings (
                job_posting_id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL,
                job_title TEXT NOT NULL, job_remote TEXT, job_description TEXT NOT NULL,
                published_date TEXT, employment_type TEXT, job_url TEXT UNIQUE,
                compensation_summary TEXT, last_scanned TEXT
            )
        """)

        class FakeDatabase(database_connection_system):
            def __init__(self):
                self.db_connection = connection
                self.log_errors = Error_Log_system()

            def get_company_slug_map(self):
                return {"board": 4}

            def bulk_insert_scrape_logs(self, rows):
                pass

        scraper = greenhouse_main_system.__new__(greenhouse_main_system)
        scraper.database = FakeDatabase()
        job = {
            "id": 3032038, "absolute_url": "https://example.test/jobs/3032038",
            "title": "Engineer", "content": "<p>Example</p>",
            "location": {"name": "Bangkok"},
        }
        other = {
            "id": 10, "absolute_url": "https://example.test/jobs/10",
            "title": "Other", "content": "<p>Example</p>",
            "location": {"name": "Pune"},
        }
        new_job = {
            "id": 11, "absolute_url": "https://example.test/jobs/11",
            "title": "Missing location", "content": "<p>Example</p>",
            "location": {"name": None}, "locations": None,
        }
        jobs = [job, other]
        scraper.slug_processing = lambda slug: {
            "company_slug": slug, "ats_vendor": "Greenhouse", "status": "success",
            "job_count": len(jobs), "error": None, "jobs": jobs,
        }
        scraper.data_aquasition(["board"])
        parse_database(connection, self.gazetteer)
        job["location"] = {"name": None}
        job["locations"] = [{"name": "Pune"}, {"name": None}]
        other["location"] = {"name": None}
        jobs.append(new_job)
        log_path = Path(self.directory.name) / "errors.txt"
        def open_log(_path, *args, **kwargs):
            return open(log_path, *args, **kwargs)

        with patch("logging_errors.open", side_effect=open_log):
            scraper.data_aquasition(["board"])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_postings").fetchone()[0], 3)
        rows = connection.execute("""
            SELECT vendor_job_id, raw_text FROM job_parsed_locations
            ORDER BY vendor_job_id, raw_text
        """).fetchall()
        self.assertEqual(rows, [("10", "Pune"), ("3032038", "Bangkok"), ("3032038", "Pune")])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 3)
        log = log_path.read_text(encoding="utf-8")
        for value in ("Greenhouse", "board", "3032038", "https://example.test/jobs/3032038", '"name": null'):
            self.assertIn(value, log)
        self.assertEqual(connection.execute("""
            SELECT COUNT(*) FROM job_raw_locations
            WHERE vendor_job_id = '11'
        """).fetchone()[0], 0)
        job["location"] = {"name": "Pune"}
        job["locations"] = None
        jobs[:] = [job]
        scraper.data_aquasition(["board"])
        self.assertEqual(connection.execute("""
            SELECT raw_text FROM job_parsed_locations
            WHERE vendor_job_id = '3032038'
        """).fetchall(), [("Pune",)])

    def test_scraper_logs_job_and_batch_errors_with_context(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("""
            CREATE TABLE job_postings (
                job_posting_id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL,
                job_title TEXT NOT NULL, job_remote TEXT, job_description TEXT NOT NULL,
                published_date TEXT, employment_type TEXT, job_url TEXT UNIQUE,
                compensation_summary TEXT, last_scanned TEXT
            )
        """)

        class FakeDatabase(database_connection_system):
            def __init__(self):
                self.db_connection = connection
                self.log_errors = Error_Log_system()

            def get_company_slug_map(self):
                return {"board": 4}

            def bulk_insert_scrape_logs(self, rows):
                pass

        scraper = greenhouse_main_system.__new__(greenhouse_main_system)
        scraper.database = FakeDatabase()
        job = {"id": None, "absolute_url": "https://example.test/123",
               "title": "Engineer", "content": "<p>Example</p>", "location": {"name": 42}}
        scraper.slug_processing = lambda slug: {
            "company_slug": slug, "ats_vendor": "Greenhouse", "status": "success",
            "job_count": 1, "error": None, "jobs": [job],
        }
        log_path = Path(self.directory.name) / "errors.txt"
        def open_log(_path, *args, **kwargs):
            return open(log_path, *args, **kwargs)

        with patch("logging_errors.open", side_effect=open_log):
            with self.assertRaises(ValueError):
                scraper.data_aquasition(["board"])
            job["id"] = 123
            job["location"] = {"name": "Bangkok"}
            job["title"] = None
            with self.assertRaises(sqlite3.IntegrityError):
                scraper.data_aquasition(["board"])
        log = log_path.read_text(encoding="utf-8")
        for value in ("Greenhouse job preparation", "Job and location batch save (candidate jobs)",
                      "Greenhouse", "board", '"company_id": 4', '"vendor_job_id": 123',
                      "https://example.test/123", "Bangkok", '"name": 42'):
            self.assertIn(value, log)
        self.assertRegex(log, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_postings").fetchone()[0], 0)

    def test_parser_error_logs_linked_job_context(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE companies (company_id INTEGER PRIMARY KEY, slug_name TEXT);
            CREATE TABLE job_postings (job_posting_id INTEGER PRIMARY KEY, company_id INTEGER, job_url TEXT UNIQUE);
            INSERT INTO companies VALUES (4, 'board');
            INSERT INTO job_postings VALUES (7, 4, 'https://example.test/7');
        """)
        self.store_locations([("https://example.test/7", "700", ["Bangkok"])], connection=connection)
        log_path = Path(self.directory.name) / "errors.txt"
        def open_log(_path, *args, **kwargs):
            return open(log_path, *args, **kwargs)

        with patch("logging_errors.open", side_effect=open_log), patch(
            "Jobs.Locations.locations_func.parse_locations", side_effect=ValueError("test parse failure")
        ):
            with self.assertRaisesRegex(ValueError, "test parse failure"):
                parse_database(connection, self.gazetteer)
        log = log_path.read_text(encoding="utf-8")
        for value in ("test parse failure", "Greenhouse", "board", '"company_id": 4',
                      '"job_posting_id": 7', '"vendor_job_id": "700"',
                      "Bangkok", "https://example.test/7"):
            self.assertIn(value, log)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM parsed_locations").fetchone()[0], 0)
        connection.execute("""
            CREATE TRIGGER reject_parsed_location BEFORE INSERT ON parsed_locations
            BEGIN SELECT RAISE(ABORT, 'parse save failed'); END
        """)
        with patch("logging_errors.open", side_effect=open_log):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "parse save failed"):
                parse_database(connection, self.gazetteer)
        log = log_path.read_text(encoding="utf-8")
        self.assertIn("parse save failed", log)
        self.assertIn('"vendor_job_id": "700"', log)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM parsed_locations").fetchone()[0], 0)

    def test_command_parses_existing_database_and_reports_missing_file(self):
        path = Path(self.directory.name) / "jobs.db"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE job_postings (job_posting_id INTEGER PRIMARY KEY, job_url TEXT UNIQUE)")
            connection.execute("INSERT INTO job_postings VALUES (7, 'https://example.test/7')")
            self.store_locations([("https://example.test/7", "700", ["Bangkok"])], connection=connection)
        command = [sys.executable, "-m", "Jobs.Locations.locations_func", "--db", str(path),
                   "--gazetteer", str(self.gazetteer)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn("Parsed 1 distinct", result.stdout)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute(
                "SELECT job_posting_id, city_name, country FROM job_parsed_locations"
            ).fetchone(), (7, "Bangkok", "TH"))
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        result = subprocess.run(command[:-1] + [str(self.gazetteer) + ".missing"],
                                capture_output=True, text=True, cwd=self.directory.name, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GeoNames file does not exist", result.stderr)
        self.assertIn("GeoNames file does not exist", (Path(self.directory.name) / "errors.txt").read_text())


if __name__ == "__main__":
    unittest.main()
