import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Jobs.lever_main import lever_algor
from Jobs.Locations.locations_func import lever_raw_locations, parse_database
from The_Database.db_connect import database_connection_system
from logging_errors import Error_Log_system


class LeverLocationTests(unittest.TestCase):
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
            INSERT INTO companies VALUES (1, 'board');
            CREATE TABLE job_postings (
                job_posting_id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL,
                job_title TEXT NOT NULL, job_remote TEXT, job_description TEXT NOT NULL,
                published_date TEXT, employment_type TEXT, job_url TEXT UNIQUE,
                compensation_summary TEXT, last_scanned TEXT
            );
        """)

        class FakeDatabase(database_connection_system):
            def __init__(self, connection):
                self.db_connection = connection
                self.log_errors = Error_Log_system()

            def get_company_slug_map(self):
                return {"board": 1}

            def bulk_insert_scrape_logs(self, rows):
                self.rows = rows

        self.scraper = lever_algor.__new__(lever_algor)
        self.scraper.database = FakeDatabase(self.connection)
        self.scraper.list_of_slugs = ["board"]
        self.jobs = []
        self.scraper.slug_processing = lambda slug: {
            "company_slug": slug, "ats_vendor": "Lever", "status": "success",
            "job_count": len(self.jobs), "error": None, "jobs": self.jobs,
        }

    def job(self, identifier, location, locations, workplace="remote"):
        return {
            "id": identifier, "applyUrl": f"https://jobs.lever.co/board/{identifier}/apply",
            "text": "Engineer", "descriptionPlain": "Description", "createdAt": 1735689600000,
            "categories": {"location": location, "allLocations": locations},
            "workplaceType": workplace,
        }

    def test_extract_all_locations_and_incomplete_data(self):
        job = self.job("a", "Bangkok", ["Bangkok", "Pune", None])
        values, issues = lever_raw_locations(job)
        self.assertEqual(values, ["Bangkok", "Pune"])
        self.assertEqual(len(issues), 1)
        self.assertTrue(lever_raw_locations({"categories": None})[1])
        self.assertTrue(lever_raw_locations({"categories": {"allLocations": "Pune"}})[1])
        self.assertEqual(lever_raw_locations({"categories": {"allLocations": ["Pune"]}}), (["Pune"], []))
        self.assertTrue(lever_raw_locations({"categories": {"location": None, "allLocations": ["Pune"]}})[1])
        self.assertTrue(lever_raw_locations({"categories": {}})[1])

    def test_scrape_parse_rescrape_and_per_job_workplace_flags(self):
        first = self.job("a", "Bangkok", ["Bangkok", "Pune"], "remote")
        second = self.job("b", "Bangkok", ["Bangkok"], "hybrid")
        self.jobs[:] = [first, second]
        self.scraper.data_aquasition()
        self.assertEqual(self.connection.execute("""
            SELECT vendor_job_id, raw_text FROM job_parsed_locations ORDER BY vendor_job_id, raw_text
        """).fetchall(), [("a", "Bangkok"), ("a", "Pune"), ("b", "Bangkok")])
        self.assertEqual(parse_database(self.connection, self.gazetteer, ats_vendor="Lever"), 2)
        self.assertEqual(self.connection.execute("""
            SELECT job_posting_id, city_name, is_remote, is_hybrid
            FROM locations ORDER BY job_posting_id, city_name
        """).fetchall(), [(1, "Bangkok", 1, 0), (1, "Pune", 1, 0), (2, "Bangkok", 0, 1)])
        self.scraper.data_aquasition()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 3)
        first["categories"] = {"location": "Pune", "allLocations": ["Pune"]}
        first["workplaceType"] = "on-site"
        self.scraper.data_aquasition()
        self.assertEqual(self.connection.execute("""
            SELECT city_name, is_remote, is_hybrid FROM locations WHERE job_posting_id = 1
        """).fetchall(), [("Pune", 0, 0)])

    def test_missing_location_is_logged_and_preserves_previous_links(self):
        first = self.job("a", "Bangkok", ["Pune"])
        self.jobs[:] = [first]
        self.scraper.data_aquasition()
        first["categories"] = {"location": None, "allLocations": ["Pune", None]}
        log_path = self.path / "errors.txt"
        def open_log(_path, *args, **kwargs):
            return open(log_path, *args, **kwargs)
        with patch("logging_errors.open", side_effect=open_log):
            self.scraper.data_aquasition()
        self.assertEqual(self.connection.execute(
            "SELECT raw_text FROM job_parsed_locations ORDER BY raw_text"
        ).fetchall(), [("Bangkok",), ("Pune",)])
        log = log_path.read_text(encoding="utf-8")
        for value in ("Lever", "board", '"vendor_job_id": "a"', "jobs.lever.co", '"location": null'):
            self.assertIn(value, log)

    def test_existing_url_matches_prior_posting_and_missing_id(self):
        job = self.job("a", "Bangkok", [])
        del job["id"]
        self.connection.execute("""
            INSERT INTO job_postings VALUES (42, 1, 'Old', NULL, 'Description', NULL, NULL, ?, NULL, NULL)
        """, (job["applyUrl"],))
        self.connection.commit()
        self.jobs[:] = [job]
        self.scraper.data_aquasition()
        self.assertEqual(self.connection.execute("""
            SELECT job_posting_id, vendor_job_id FROM job_raw_locations
        """).fetchone(), (42, job["applyUrl"]))

    def test_command_supports_lever_and_default(self):
        job = self.job("a", "Bangkok", ["Pune"])
        path = self.path / "jobs.db"
        self.jobs[:] = [job]
        self.scraper.data_aquasition()
        destination = sqlite3.connect(path)
        self.connection.backup(destination)
        destination.close()
        command = [sys.executable, "-m", "Jobs.Locations.locations_func", "--db", str(path),
                   "--gazetteer", str(self.gazetteer)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn("Parsed 2 distinct Lever location strings.", result.stdout)
        result = subprocess.run(command + ["--vendor", "Lever", "--reparse"],
                                capture_output=True, text=True, check=True)
        self.assertIn("Parsed 2 distinct Lever location strings.", result.stdout)
        self.assertNotIn("Greenhouse", result.stdout)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
