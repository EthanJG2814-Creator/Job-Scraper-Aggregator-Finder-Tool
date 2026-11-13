import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from bs4 import BeautifulSoup

from Jobs.icims_main import icims_main_system
from Jobs.Locations.locations_func import icims_raw_locations, parse_database
from The_Database.db_connect import database_connection_system
from logging_errors import Error_Log_system


def page(posting, header="", content=True):
    script = f'<script type="application/ld+json">{json.dumps(posting)}</script>'
    description = '<div class="iCIMS_JobContent"><div class="iCIMS_InfoMsg_Job">Description</div></div>' if content else ""
    return script + header + description


def address(city, region, country):
    return {"address": {"addressLocality": city, "addressRegion": region, "addressCountry": country}}


class ICIMSLocationTests(unittest.TestCase):
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

        self.scraper = icims_main_system.__new__(icims_main_system)
        self.scraper.database = FakeDatabase(self.connection)
        self.scraper.logging_errors = Error_Log_system()
        self.jobs = []
        self.scraper.slug_processing = lambda slug: {
            "company_slug": slug, "ats_vendor": "iCIMS", "status": "success",
            "job_count": len(self.jobs), "error": None, "jobs": self.jobs,
        }

    def job(self, identifier, locations, issues=(), workplace=None):
        return {
            "id": identifier, "title": "Engineer", "description": "Description", "remote": False,
            "url": f"https://careers-board.icims.com/jobs/{identifier}/engineer/job",
            "locations": locations, "location_issues": list(issues), "workplace_type": workplace,
        }

    def test_extract_multiple_addresses_graph_and_remote(self):
        posting = {"@graph": [{"@type": ["Thing", "JobPosting"], "jobLocationType": "TELECOMMUTE",
                              "jobLocation": [address("Austin", "TX", "US"),
                                              address("Vancouver", "BC", "CA"),
                                              address("Austin", "TX", "US")]}]}
        result = icims_raw_locations(BeautifulSoup(page(posting), "html.parser"))
        self.assertEqual(result, (["Austin, TX, US", "Vancouver, BC, CA"], [], "Remote"))
        remote = {"@type": "JobPosting", "jobLocationType": "TELECOMMUTE"}
        self.assertEqual(icims_raw_locations(BeautifulSoup(page(remote), "html.parser")),
                         (["Remote"], [], "Remote"))
        unavailable = {"@type": "JobPosting", "jobLocation": address("UNAVAILABLE", "UNAVAILABLE", "US")}
        self.assertEqual(icims_raw_locations(BeautifulSoup(page(unavailable), "html.parser")),
                         (["US"], [], None))

    def test_header_fallback_invalid_and_missing(self):
        header = '<div><span class="sr-only field-label">Job Locations</span><span>US-TX-Austin</span></div>'
        self.assertEqual(icims_raw_locations(BeautifulSoup(header, "html.parser")),
                         (["US-TX-Austin"], [], None))
        invalid = page({"@type": "JobPosting", "jobLocation": [address("Austin", "TX", "US"), None]})
        values, issues, _ = icims_raw_locations(BeautifulSoup(invalid, "html.parser"))
        self.assertEqual(values, ["Austin, TX, US"])
        self.assertTrue(issues)
        self.assertTrue(icims_raw_locations(BeautifulSoup("<html></html>", "html.parser"))[1])
        broken = '<script type="application/ld+json">{invalid</script>' + header
        values, issues, _ = icims_raw_locations(BeautifulSoup(broken, "html.parser"))
        self.assertEqual(values, ["US-TX-Austin"])
        self.assertTrue(issues)

    def test_one_detail_request_captures_description_and_locations(self):
        url = "https://careers-board.icims.com/jobs/42/engineer/job?x=1"
        posting = {"@type": "JobPosting", "jobLocation": [address("Austin", "TX", "US")]}
        response = requests.Response()
        response.status_code = 200
        response._content = page(posting).encode()
        with patch("Jobs.icims_main.requests.Session") as session_class:
            session = session_class.return_value
            session.get.return_value = response
            result = self.scraper._fetch_job_details(url, session)
        self.assertEqual(result, ("Description", ["Austin, TX, US"], [], None))
        session.get.assert_called_once_with(url + "&in_iframe=1", timeout=20)

    def test_sitemap_to_database_parse_and_rescrape(self):
        url = "https://careers-board.icims.com/jobs/42/engineer/job"
        posting = {"@type": "JobPosting", "jobLocationType": "TELECOMMUTE",
                   "jobLocation": [address("Austin", "TX", "US"), address("Vancouver", "BC", "CA")]}
        response = requests.Response()
        response.status_code = 200
        response._content = page(posting).encode()
        with patch.object(self.scraper, "_fetch_sitemap_urls", return_value=[url]):
            with patch("Jobs.icims_main.requests.Session") as session_class:
                session = session_class.return_value
                session.get.return_value = response
                self.jobs[:] = self.scraper._fetch_sitemap_jobs("sitemap", session)
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute(
            "SELECT vendor_job_id, raw_text FROM job_parsed_locations ORDER BY raw_text"
        ).fetchall(), [("42", "Austin, TX, US"), ("42", "Vancouver, BC, CA")])
        self.assertEqual(parse_database(self.connection, self.gazetteer, ats_vendor="iCIMS"), 2)
        self.assertEqual(self.connection.execute(
            "SELECT city_name, state_abbv, country, is_remote FROM locations ORDER BY city_name"
        ).fetchall(), [("Austin", "TX", "US", 1), ("Vancouver", "BC", "CA", 1)])
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)
        self.jobs[0]["locations"] = ["Austin, TX, US"]
        self.jobs[0]["workplace_type"] = None
        self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute(
            "SELECT city_name, is_remote FROM locations"
        ).fetchall(), [("Austin", 0)])

    def test_incomplete_response_preserves_links_and_logs_context(self):
        job = self.job("42", ["Austin, TX, US", "Vancouver, BC, CA"])
        self.jobs[:] = [job]
        self.scraper.data_aquasition(["board"])
        job["locations"] = ["Austin, TX, US"]
        job["location_issues"] = ["jobLocation has an invalid address: None"]
        log_path = self.path / "errors.txt"
        with patch("logging_errors.open", side_effect=lambda _path, *args, **kwargs: open(log_path, *args, **kwargs)):
            self.scraper.data_aquasition(["board"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM job_raw_locations").fetchone()[0], 2)
        log = log_path.read_text(encoding="utf-8")
        for value in ("iCIMS", "board", '"vendor_job_id": "42"', "invalid address", "jobs/42"):
            self.assertIn(value, log)

    def test_request_failure_is_incomplete(self):
        with patch("Jobs.icims_main.requests.Session") as session_class:
            session = session_class.return_value
            session.get.side_effect = requests.ConnectionError("failed")
            with patch.object(self.scraper.logging_errors, "icims_error_catching"):
                description, locations, issues, workplace = self.scraper._fetch_job_details(
                    "https://example.test/jobs/42/test/job", session
                )
        self.assertEqual((description, locations, workplace), ("N/A", [], None))
        self.assertIn("failed", issues[0])

    def test_command_supports_icims_and_default(self):
        self.jobs[:] = [self.job("42", ["Austin, TX, US"])]
        self.scraper.data_aquasition(["board"])
        path = self.path / "jobs.db"
        destination = sqlite3.connect(path)
        self.connection.backup(destination)
        destination.close()
        command = [sys.executable, "-m", "Jobs.Locations.locations_func", "--db", str(path),
                   "--gazetteer", str(self.gazetteer)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn("Parsed 1 distinct iCIMS location strings.", result.stdout)
        result = subprocess.run(command + ["--vendor", "iCIMS", "--reparse"],
                                capture_output=True, text=True, check=True)
        self.assertIn("Parsed 1 distinct iCIMS location strings.", result.stdout)
        self.assertNotIn("Greenhouse", result.stdout)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 1)

    def test_empty_second_sitemap_does_not_discard_first(self):
        scraper = icims_main_system.__new__(icims_main_system)
        job = self.job("42", ["Austin, TX, US"])
        with patch.object(scraper, "_fetch_sitemap_jobs", side_effect=[[job], []]):
            result = scraper.slug_processing("board")
        self.assertEqual(result["jobs"], [job])

    def test_sitemaps_contribute_unique_job_ids(self):
        scraper = icims_main_system.__new__(icims_main_system)
        first = self.job("42", ["Austin, TX, US"])
        second = self.job("43", ["Vancouver, BC, CA"])
        with patch.object(scraper, "_fetch_sitemap_jobs", side_effect=[[first], [first, second]]):
            result = scraper.slug_processing("board")
        self.assertEqual(result["jobs"], [first, second])

    def test_duplicate_url_from_two_slugs_saves_all_locations(self):
        first = self.job("42", ["Austin, TX, US"])
        second = self.job("42", ["Austin, TX, US", "Vancouver, BC, CA"])
        results = [
            {"company_slug": "board", "ats_vendor": "iCIMS", "status": "success",
             "job_count": 1, "error": None, "jobs": [job]}
            for job in (first, second)
        ]
        self.scraper._save_results(results, {"board": 1})
        self.assertEqual(self.connection.execute(
            "SELECT vendor_job_id, raw_text FROM job_parsed_locations ORDER BY raw_text"
        ).fetchall(), [("42", "Austin, TX, US"), ("42", "Vancouver, BC, CA")])

    def test_completed_batches_persist_when_later_batch_fails(self):
        first, second = self.job("42", ["Austin, TX, US"]), self.job("43", ["Vancouver, BC, CA"])
        results = [
            {"company_slug": "board", "ats_vendor": "iCIMS", "status": "success",
             "job_count": 1, "error": None, "jobs": [job]}
            for job in (first, second)
        ]
        calls = iter(results)
        self.scraper.slug_processing = lambda slug: next(calls)
        original_save = self.scraper.database.bulk_insert_jobs_with_locations
        save_count = 0

        def fail_second(*args):
            nonlocal save_count
            save_count += 1
            if save_count == 2:
                raise sqlite3.OperationalError("simulated failure")
            return original_save(*args)

        self.scraper.database.bulk_insert_jobs_with_locations = fail_second
        with patch("Jobs.icims_main.RESULT_BATCH_SIZE", 1), \
             patch("Jobs.icims_main.as_completed", side_effect=lambda futures: futures):
            with self.assertRaisesRegex(sqlite3.OperationalError, "simulated failure"):
                self.scraper.data_aquasition(["board", "board"])
        self.assertEqual(self.connection.execute("SELECT job_url FROM job_postings").fetchall(),
                         [(first["url"],)])
        self.assertEqual(self.connection.execute("SELECT vendor_job_id FROM job_raw_locations").fetchall(),
                         [("42",)])


if __name__ == "__main__":
    unittest.main()
