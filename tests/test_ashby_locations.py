import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Jobs.ashby_main import Ashby_Algor
from Jobs.Locations.locations_func import ashby_raw_locations, initialize_location_schema, parse_database, store_locations
from The_Database.db_connect import database_connection_system
from logging_errors import Error_Log_system


class AshbyLocationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.gazetteer = self.path / "cities15000.txt"
        self.gazetteer.write_text(
            "1\tBangkok\tBangkok\t\t0\t0\tP\tPPL\tTH\t\t40\t\t\t\t100000\t\t\tUTC\t2024-01-01\n"
            "2\tPune\tPune\t\t0\t0\tP\tPPL\tIN\t\t16\t\t\t\t100000\t\t\tUTC\t2024-01-01\n",
            encoding="utf-8",
        )
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE companies (company_id INTEGER PRIMARY KEY, slug_name TEXT);
            CREATE TABLE job_postings (
                job_posting_id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL,
                job_title TEXT NOT NULL, job_remote TEXT, job_description TEXT NOT NULL,
                published_date TEXT, employment_type TEXT, job_url TEXT UNIQUE,
                compensation_summary TEXT, last_scanned TEXT
            );
            INSERT INTO companies VALUES (1, 'board');
        """)

        class FakeDatabase(database_connection_system):
            def __init__(self, connection):
                self.db_connection = connection
                self.log_errors = Error_Log_system()

            def get_company_slug_map(self):
                return {"board": 1}

            def bulk_insert_scrape_logs(self, rows):
                self.rows = rows

        self.scraper = Ashby_Algor.__new__(Ashby_Algor)
        self.scraper.database = FakeDatabase(self.connection)
        self.jobs = []
        self.scraper.slug_processing = lambda slug: {
            "company_slug": slug, "ats_vendor": "Ashby", "status": "success",
            "job_count": len(self.jobs), "error": None, "jobs": self.jobs,
        }
        self.log_path = self.path / "errors.txt"

    def job(self, identifier, primary, secondary=(), workplace="OnSite"):
        return {
            "id": identifier, "jobUrl": f"https://jobs.ashbyhq.com/board/{identifier}",
            "title": "Engineer", "descriptionPlain": "Description", "isRemote": workplace != "OnSite",
            "location": primary, "secondaryLocations": [{"location": item} for item in secondary],
            "workplaceType": workplace,
        }

    def test_primary_secondary_duplicates_and_missing(self):
        values, issues = ashby_raw_locations({
            "location": " Bangkok ",
            "secondaryLocations": [{"location": "Pune"}, {"location": "Pune"}, {"location": None}],
        })
        self.assertEqual(values, [" Bangkok ", "Pune"])
        self.assertEqual(len(issues), 1)
        self.assertTrue(ashby_raw_locations({"location": None, "secondaryLocations": "invalid"})[1])

    def test_end_to_end_and_workplace_flags_per_job(self):
        self.jobs[:] = [self.job("a", "Bangkok", ["Pune", "Bangkok"], "Remote"),
                        self.job("b", "Bangkok", workplace="Hybrid")]
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute("""
            SELECT vendor_job_id, raw_text FROM job_parsed_locations
            ORDER BY vendor_job_id, raw_text
        """).fetchall(), [("a", "Bangkok"), ("a", "Pune"), ("b", "Bangkok")])
        self.assertEqual(parse_database(self.connection, self.gazetteer, ats_vendor="Ashby"), 2)
        self.assertEqual(self.connection.execute("""
            SELECT j.vendor_job_id, p.city_name, p.is_remote, p.is_hybrid
            FROM job_parsed_locations p JOIN job_raw_locations j
              ON j.job_posting_id = p.job_posting_id AND j.raw_location_id = (
                  SELECT raw_location_id FROM raw_locations WHERE ats_vendor = p.ats_vendor AND raw_text = p.raw_text
              )
            ORDER BY j.vendor_job_id, p.city_name
        """).fetchall(), [("a", "Bangkok", 1, 0), ("a", "Pune", 1, 0), ("b", "Bangkok", 0, 1)])
        self.assertEqual(self.connection.execute("""
            SELECT job_posting_id, city_name, is_remote, is_hybrid FROM locations ORDER BY job_posting_id, city_name
        """).fetchall(), [(1, "Bangkok", 1, 0), (1, "Pune", 1, 0), (2, "Bangkok", 0, 1)])
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 3)
        self.jobs[0]["workplaceType"] = "Hybrid"
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute("""
            SELECT DISTINCT is_remote, is_hybrid FROM locations WHERE job_posting_id = 1
        """).fetchall(), [(0, 1)])
        self.assertEqual(parse_database(self.connection, self.gazetteer, reparse=True, ats_vendor="Ashby"), 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 3)

    def test_incomplete_preserves_existing_and_saves_other_locations(self):
        job = self.job("a", "Bangkok", ["Pune"])
        self.jobs[:] = [job]
        self.scraper.data_aquasition(["board"])
        job["location"] = None
        job["secondaryLocations"] = [{"location": "Pune"}, {"location": "Thailand"}]
        def open_log(_path, *args, **kwargs):
            return open(self.log_path, *args, **kwargs)
        with patch("logging_errors.open", side_effect=open_log):
            self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute("""
            SELECT raw_text FROM job_parsed_locations ORDER BY raw_text
        """).fetchall(), [("Bangkok",), ("Pune",), ("Thailand",)])
        log = self.log_path.read_text(encoding="utf-8")
        for value in ("Ashby", "board", '"vendor_job_id": "a"', '"location": null', "jobs.ashbyhq.com"):
            self.assertIn(value, log)

    def test_missing_vendor_id_uses_posting_url_as_identifier(self):
        job = self.job("a", "Bangkok")
        del job["id"]
        self.jobs[:] = [job]
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute(
            "SELECT vendor_job_id FROM job_raw_locations"
        ).fetchone()[0], job["jobUrl"])

    def test_existing_link_schema_migrates_without_losing_records(self):
        self.connection.executescript("""
            CREATE TABLE raw_locations (raw_location_id INTEGER PRIMARY KEY, ats_vendor TEXT NOT NULL, raw_text TEXT NOT NULL, UNIQUE(ats_vendor, raw_text));
            CREATE TABLE job_raw_locations (
                job_posting_id INTEGER NOT NULL, ats_vendor TEXT NOT NULL, vendor_job_id TEXT NOT NULL,
                raw_location_id INTEGER NOT NULL, PRIMARY KEY(job_posting_id, ats_vendor, raw_location_id)
            );
            INSERT INTO job_postings VALUES (7, 1, 'Existing', NULL, 'Desc', NULL, NULL, 'existing', NULL, NULL);
            INSERT INTO raw_locations VALUES (1, 'Greenhouse', 'Bangkok');
            INSERT INTO job_raw_locations VALUES (7, 'Greenhouse', 'old', 1);
            CREATE VIEW job_parsed_locations AS SELECT vendor_job_id FROM job_raw_locations;
        """)
        initialize_location_schema(self.connection)
        self.assertIn("workplace_type", [row[1] for row in self.connection.execute("PRAGMA table_info(job_raw_locations)")])
        self.assertEqual(self.connection.execute("SELECT vendor_job_id FROM job_parsed_locations").fetchone()[0], "old")
        self.assertEqual(parse_database(self.connection, self.gazetteer), 1)
        self.assertEqual(self.connection.execute("SELECT city_name FROM locations").fetchone()[0], "Bangkok")

    def test_command_parses_both_vendors_and_selective_reparse(self):
        path = self.path / "jobs.db"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE job_postings (job_posting_id INTEGER PRIMARY KEY, job_url TEXT UNIQUE)")
            connection.executemany("INSERT INTO job_postings VALUES (?, ?)", [(1, "url1"), (2, "url2")])
            initialize_location_schema(connection)
            with connection:
                store_locations(connection, "Ashby", [("url1", "a", ["Pune"])])
                store_locations(connection, "Greenhouse", [("url2", "g", ["Bangkok"])])
        command = [sys.executable, "-m", "Jobs.Locations.locations_func", "--db", str(path),
                   "--gazetteer", str(self.gazetteer)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn("Parsed 1 distinct Ashby location strings.", result.stdout)
        self.assertIn("Parsed 1 distinct Greenhouse location strings.", result.stdout)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)
        result = subprocess.run(command + ["--vendor", "Ashby", "--reparse"],
                                capture_output=True, text=True, check=True)
        self.assertIn("Parsed 1 distinct Ashby location strings.", result.stdout)
        self.assertNotIn("Greenhouse", result.stdout)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
