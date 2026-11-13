import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Jobs.bamboohr_main import bamboohr_main_system
from Jobs.Locations.locations_func import bamboohr_raw_locations, parse_database
from The_Database.db_connect import database_connection_system
from logging_errors import Error_Log_system


class BambooHRLocationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.gazetteer = self.path / "cities15000.txt"
        self.gazetteer.write_text(
            "1\tAustin\tAustin\t\t0\t0\tP\tPPL\tUS\t\tTX\t\t\t\t100000\t\t\tUTC\t2024-01-01\n"
            "2\tVancouver\tVancouver\t\t0\t0\tP\tPPL\tCA\t\t02\t\t\t\t100000\t\t\tUTC\t2024-01-01\n",
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

        self.scraper = bamboohr_main_system.__new__(bamboohr_main_system)
        self.scraper.database = FakeDatabase(self.connection)
        self.jobs = []
        self.scraper.slug_processing = lambda slug: {
            "company_slug": slug, "ats_vendor": "BambooHR", "status": "success",
            "job_count": len(self.jobs), "error": None, "jobs": self.jobs,
        }

    def job(self, identifier, location_type="0"):
        return {
            "_bamboohr_job_id": identifier,
            "jobOpeningShareUrl": f"https://board.bamboohr.com/careers/{identifier}",
            "jobOpeningName": "Engineer", "description": "<p>Description</p>",
            "location": {"city": "Austin", "state": "Texas", "addressCountry": "United States"},
            "atsLocation": {"city": "Vancouver", "state": "British Columbia", "country": "Canada"},
            "locationType": location_type,
        }

    def test_extract_structured_addresses_and_missing(self):
        job = self.job(23)
        self.assertEqual(bamboohr_raw_locations(job), (
            ["Austin, Texas, United States", "Vancouver, British Columbia, Canada"], []
        ))
        job["atsLocation"] = {"city": "Austin", "state": "Texas", "country": "United States"}
        self.assertEqual(len(bamboohr_raw_locations(job)[0]), 1)
        self.assertEqual(bamboohr_raw_locations({"locationType": "1"}), (["Remote"], []))
        self.assertTrue(bamboohr_raw_locations({"location": None, "locationType": "0"})[1])
        self.assertTrue(bamboohr_raw_locations({"location": {"city": 42}})[1])
        self.assertTrue(bamboohr_raw_locations({"atsLocation": "bad"})[1])
        self.assertEqual(bamboohr_raw_locations(
            {"location": {"city": 42, "state": "Texas", "addressCountry": "United States"}}
        )[0], [])

    def test_scrape_parse_repeat_and_per_job_workplace(self):
        first, second = self.job(23, "1"), self.job(24, 2)
        second["atsLocation"] = None
        self.jobs[:] = [first, second]
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute(
            "SELECT vendor_job_id, raw_text FROM job_parsed_locations ORDER BY vendor_job_id, raw_text"
        ).fetchall(), [
            ("23", "Austin, Texas, United States"),
            ("23", "Vancouver, British Columbia, Canada"),
            ("24", "Austin, Texas, United States"),
        ])
        self.assertEqual(parse_database(self.connection, self.gazetteer, ats_vendor="BambooHR"), 2)
        self.assertEqual(self.connection.execute(
            "SELECT city_name, state_abbv, country, is_remote, is_hybrid FROM locations ORDER BY job_posting_id, city_name"
        ).fetchall(), [("Austin", "TX", "US", 1, 0), ("Vancouver", "BC", "CA", 1, 0),
                      ("Austin", "TX", "US", 0, 1)])
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 3)
        first["location"] = {"city": "Vancouver", "state": "British Columbia", "addressCountry": "Canada"}
        first["locationType"] = "0"
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute(
            "SELECT city_name, is_remote FROM locations WHERE job_posting_id = 1"
        ).fetchall(), [("Vancouver", 0)])

    def test_malformed_location_is_logged_and_preserves_old_links(self):
        job = self.job(23)
        self.jobs[:] = [job]
        self.scraper.data_aquasition(["board"])
        job["location"] = None
        job["atsLocation"]["city"] = 42
        log_path = self.path / "errors.txt"
        with patch("logging_errors.open", side_effect=lambda _path, *args, **kwargs: open(log_path, *args, **kwargs)):
            self.scraper.data_aquasition(["board"])
        self.assertIn(("Austin, Texas, United States",), self.connection.execute(
            "SELECT raw_text FROM job_parsed_locations"
        ).fetchall())
        log = log_path.read_text(encoding="utf-8")
        for value in ("BambooHR", "board", '"vendor_job_id": 23', "atsLocation", "careers/23"):
            self.assertIn(value, log)

    def test_missing_id_falls_back_to_existing_url(self):
        job = self.job(23)
        del job["_bamboohr_job_id"]
        self.connection.execute(
            "INSERT INTO job_postings VALUES (42, 1, 'Old', NULL, 'Description', NULL, NULL, ?, NULL, NULL)",
            (job["jobOpeningShareUrl"],),
        )
        self.connection.commit()
        self.jobs[:] = [job]
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute(
            "SELECT DISTINCT job_posting_id, vendor_job_id FROM job_raw_locations"
        ).fetchall(), [(42, job["jobOpeningShareUrl"])])

    def test_remote_without_address_is_materialized(self):
        job = self.job(23, "1")
        job["location"] = {"city": None, "state": None, "addressCountry": None}
        job["atsLocation"] = None
        self.jobs[:] = [job]
        self.scraper.data_aquasition(["board"])
        self.assertEqual(parse_database(self.connection, self.gazetteer, ats_vendor="BambooHR"), 1)
        self.assertEqual(self.connection.execute(
            "SELECT city_name, country, is_remote FROM locations"
        ).fetchall(), [(None, None, 1)])
        self.assertTrue(self.scraper.get_remote_status(job))

    def test_listing_id_is_carried_through_detail_fetch(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": [{"id": 23}]}

        scraper = bamboohr_main_system.__new__(bamboohr_main_system)
        scraper.fetch_job_detail = lambda slug, identifier: {"jobOpeningName": "Engineer"}
        with patch("Jobs.bamboohr_main.requests.get", return_value=Response()):
            result = scraper.slug_processing("board")
        self.assertEqual(result["jobs"][0]["_bamboohr_job_id"], 23)

    def test_command_supports_bamboohr_and_default(self):
        self.jobs[:] = [self.job(23)]
        self.scraper.data_aquasition(["board"])
        path = self.path / "jobs.db"
        destination = sqlite3.connect(path)
        self.connection.backup(destination)
        destination.close()
        command = [sys.executable, "-m", "Jobs.Locations.locations_func", "--db", str(path),
                   "--gazetteer", str(self.gazetteer)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn("Parsed 2 distinct BambooHR location strings.", result.stdout)
        result = subprocess.run(command + ["--vendor", "BambooHR", "--reparse"],
                                capture_output=True, text=True, check=True)
        self.assertIn("Parsed 2 distinct BambooHR location strings.", result.stdout)
        self.assertNotIn("Greenhouse", result.stdout)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
