import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

import requests

from Jobs.Locations.locations_func import parse_database, workday_raw_locations
from Jobs.workday_alt import workday_alt_algor
from Jobs.workday_main import workday_algor
from The_Database.db_connect import database_connection_system
from logging_errors import Error_Log_system


class WorkdayLocationTests(unittest.TestCase):
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
            INSERT INTO companies VALUES (1, 'board|wd1|jobs');
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
                self.cursor = connection.cursor()
                self.log_errors = Error_Log_system()

            def bulk_insert_scrape_logs(self, rows):
                self.rows = rows

        self.scraper = workday_algor.__new__(workday_algor)
        self.scraper.database = FakeDatabase(self.connection)
        self.scraper.known_urls = set()
        self.scraper.located_urls = set()
        self.scraper.failed_saves = set()
        self.scraper.save_queue = Queue()
        self.slug = "board|wd1|jobs"
        self.url = "https://board.wd1.myworkdayjobs.com/jobs/job/Austin/Engineer_R42"

    def detail(self, location="Austin, TX", additional=None, remote="Hybrid"):
        info = {"title": "Engineer", "jobDescription": "<p>Description</p>",
                "location": location, "remoteType": remote}
        if additional is not None:
            info["additionalLocations"] = additional
        return info

    def prepare(self, info, summary=None):
        self.scraper.request_workday_page = lambda *args: {"jobPostingInfo": info}
        job = self.scraper.get_job_detail("api", self.url, summary or {}, {}, {})
        job.update(company_slug=self.slug, vendor_job_id="Engineer_R42")
        return job

    def test_api_multiple_locations_and_incomplete_inputs(self):
        values, issues = workday_raw_locations(
            self.detail(additional=["Vancouver, BC", "Austin, TX"]), {"locationsText": "3 Locations"}
        )
        self.assertEqual(values, ["Austin, TX", "Vancouver, BC"])
        self.assertEqual(issues, [])
        values, issues = workday_raw_locations(
            self.detail(additional=["Vancouver, BC", None]), {"locationsText": "4 Locations"}
        )
        self.assertEqual(values, ["Austin, TX", "Vancouver, BC"])
        self.assertEqual(len(issues), 2)
        values, issues = workday_raw_locations({}, {"locationsText": "8 Locations"})
        self.assertEqual(values, [])
        self.assertTrue(issues)
        self.assertTrue(workday_raw_locations({"additionalLocations": "bad"}, {})[1])
        values, issues = workday_raw_locations({"additionalLocations": ["Vancouver, BC"]}, {})
        self.assertEqual(values, ["Vancouver, BC"])
        self.assertIn("Primary location is missing", issues)

    def test_new_and_existing_postings_saved_and_parsed(self):
        self.connection.execute("""
            INSERT INTO job_postings VALUES (42, 1, 'Old', NULL, 'Old description', NULL, NULL, ?, NULL, NULL)
        """, (self.url,))
        self.connection.commit()
        job = self.prepare(self.detail(additional=["Vancouver, BC"]), {"locationsText": "2 Locations"})
        self.scraper.save_jobs(1, [job], [])
        self.assertEqual(parse_database(self.connection, self.gazetteer, ats_vendor="Workday"), 2)
        self.assertEqual(self.connection.execute("""
            SELECT job_posting_id, city_name, country, is_hybrid FROM locations ORDER BY city_name
        """).fetchall(), [(42, "Austin", "US", 1), (42, "Vancouver", "CA", 1)])
        self.assertEqual(self.scraper.database.get_urls_with_complete_locations("Workday"), {self.url})
        self.scraper.save_jobs(1, [job], [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 2)

    def test_incomplete_preserves_previous_and_remains_retryable(self):
        complete = self.prepare(self.detail(additional=["Vancouver, BC"]), {"locationsText": "2 Locations"})
        self.scraper.save_jobs(1, [complete], [])
        incomplete = self.prepare(self.detail(additional=[None]), {"locationsText": "2 Locations"})
        self.scraper.save_jobs(1, [incomplete], [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM job_raw_locations").fetchone()[0], 2)
        self.assertEqual(self.scraper.database.get_urls_with_complete_locations("Workday"), set())
        self.assertNotIn(self.url, self.scraper.located_urls)
        self.scraper.save_jobs(1, [complete], [])
        self.assertEqual(self.scraper.database.get_urls_with_complete_locations("Workday"), {self.url})

    def test_completeness_update_is_rolled_back_with_failed_save(self):
        complete = self.prepare(self.detail())
        self.scraper.save_jobs(1, [complete], [])
        broken = dict(complete, job_title=None, location_issues=["bad address"])
        log_path = self.path / "errors.txt"
        with patch("logging_errors.open", side_effect=lambda _path, *args, **kwargs: open(log_path, *args, **kwargs)):
            with self.assertRaises(sqlite3.IntegrityError):
                self.scraper.save_jobs(1, [broken], [])
        self.assertEqual(self.scraper.database.get_urls_with_complete_locations("Workday"), {self.url})
        self.assertIn(self.url, log_path.read_text(encoding="utf-8"))

    def test_alt_page_and_multiple_location_api_fallback(self):
        scraper = workday_alt_algor.__new__(workday_alt_algor)
        scraper.page_hits = scraper.api_fallbacks = 0
        page = {"@type": "JobPosting", "title": "Engineer", "description": "Description",
                "jobLocation": {"address": {"addressLocality": "Austin", "addressRegion": "TX",
                                            "addressCountry": "US"}}}
        scraper.request_job_page = lambda *args: (
            '<script type="application/ld+json">' + json.dumps(page) + "</script>"
        )
        scraper.request_workday_page = lambda *args: {
            "jobPostingInfo": self.detail(additional=["Vancouver, BC"])
        }
        single = scraper.get_job_detail("api", self.url, {"locationsText": "Austin, TX"}, {}, {})
        self.assertEqual(single["raw_locations"], ["Austin, TX, US"])
        multi = scraper.get_job_detail("api", self.url, {"locationsText": "2 Locations"}, {}, {})
        self.assertEqual(multi["raw_locations"], ["Austin, TX", "Vancouver, BC"])
        self.assertEqual(multi["workplace_type"], "Hybrid")
        self.assertEqual((scraper.page_hits, scraper.api_fallbacks), (1, 1))

    def test_alt_telecommute_page_uses_api_to_distinguish_hybrid(self):
        scraper = workday_alt_algor.__new__(workday_alt_algor)
        scraper.page_hits = scraper.api_fallbacks = 0
        page = {"@type": "JobPosting", "title": "Engineer", "description": "Description",
                "jobLocationType": "TELECOMMUTE",
                "jobLocation": {"address": {"addressLocality": "Austin", "addressRegion": "TX",
                                            "addressCountry": "US"}}}
        scraper.request_job_page = lambda *args: (
            '<script type="application/ld+json">' + json.dumps(page) + "</script>"
        )
        scraper.request_workday_page = lambda *args: {"jobPostingInfo": self.detail(remote="Hybrid")}
        result = scraper.get_job_detail("api", self.url, {"locationsText": "Austin, TX"}, {}, {})
        self.assertEqual(result["workplace_type"], "Hybrid")

    def test_alt_remote_without_geography_uses_page_remote_signal(self):
        scraper = workday_alt_algor.__new__(workday_alt_algor)
        scraper.page_hits = scraper.api_fallbacks = 0
        page = {"@type": "JobPosting", "title": "Engineer", "description": "Description",
                "jobLocationType": "TELECOMMUTE"}
        scraper.request_job_page = lambda *args: (
            '<script type="application/ld+json">' + json.dumps(page) + "</script>"
        )
        scraper.request_workday_page = lambda *args: {
            "jobPostingInfo": {"title": "Engineer", "jobDescription": "Description"}
        }
        result = scraper.get_job_detail("api", self.url, {}, {}, {})
        self.assertEqual((result["raw_locations"], result["location_issues"],
                          result["workplace_type"], result["job_remote"]), (["Remote"], [], "Remote", 1))

    def test_existing_job_without_completed_fetch_is_backfilled(self):
        summary = {"externalPath": "/job/Austin/Engineer_R42", "title": "Engineer",
                   "locationsText": "2 Locations"}
        self.scraper.known_urls = {self.url}
        self.scraper.request_workday_page = lambda url, headers, ctx, *args: (
            {"jobPostings": [summary], "total": 1} if url.endswith("/jobs") else
            {"jobPostingInfo": self.detail(additional=["Vancouver, BC"])}
        )
        result = self.scraper.slug_processing(self.slug)
        _, jobs, known = self.scraper.save_queue.get_nowait()
        self.assertEqual(result["status"], "success")
        self.assertEqual(jobs[0]["vendor_job_id"], "Engineer_R42")
        self.assertEqual(jobs[0]["raw_locations"], ["Austin, TX", "Vancouver, BC"])
        self.assertEqual(known, [])
        self.scraper.located_urls = {self.url}
        self.scraper.slug_processing(self.slug)
        _, jobs, known = self.scraper.save_queue.get_nowait()
        self.assertEqual(jobs, [])
        self.assertEqual(known, [self.url])

    def test_failed_existing_detail_is_logged_and_retryable(self):
        summary = {"externalPath": "/job/Austin/Engineer_R42", "locationsText": "2 Locations"}
        self.scraper.known_urls = {self.url}
        def request(url, headers, ctx, *args):
            if url.endswith("/jobs"):
                return {"jobPostings": [summary], "total": 1}
            raise requests.ConnectionError("detail failed")
        self.scraper.request_workday_page = request
        log_path = self.path / "errors.txt"
        with patch("logging_errors.open", side_effect=lambda _path, *args, **kwargs: open(log_path, *args, **kwargs)):
            result = self.scraper.slug_processing(self.slug)
        self.assertEqual(result["status"], "partial")
        self.assertIn("detail failed", log_path.read_text(encoding="utf-8"))
        self.assertEqual(self.scraper.save_queue.get_nowait()[2], [self.url])

    def test_failed_page_save_marks_slug_partial(self):
        self.scraper.failed_saves.add(self.slug)
        result = {"company_slug": self.slug, "ats_vendor": "Workday", "status": "success",
                  "job_count": 1, "error": None}
        self.assertFalse(self.scraper.save_result({self.slug: 1}, result))
        self.assertEqual(self.scraper.database.rows[0][2:], ("partial", 1, "LocationSaveFailed"))

    def test_backfill_flag_revisits_today_but_still_skips_dead(self):
        self.scraper.database.get_company_slug_map = lambda: {self.slug: 1}
        self.scraper.database.get_slugs_scraped_today = lambda vendor: {self.slug}
        self.scraper.database.get_dead_slugs = lambda vendor: {"dead|wd1|jobs"}
        self.scraper.database.get_known_job_urls = lambda: set()
        self.scraper.jobs_worked = self.scraper.jobs_failed = 0
        self.scraper.limiters = SimpleNamespace(
            get=lambda host: SimpleNamespace(on_clean_slug=lambda: None),
            overall_rate=lambda: 0.0,
        )
        visited = []
        def process(slug):
            visited.append(slug)
            return {"company_slug": slug, "ats_vendor": "Workday", "status": "success",
                    "job_count": 0, "error": None, "hit_429": False}
        self.scraper.slug_processing = process
        self.scraper.list_of_slugs = [self.slug, "dead|wd1|jobs"]
        self.scraper.data_aquasition()
        self.assertEqual(visited, [])
        self.scraper.list_of_slugs = [self.slug, "dead|wd1|jobs"]
        self.scraper.data_aquasition(backfill_locations=True)
        self.assertEqual(visited, [self.slug])

    def test_cli_default_and_selective_reparse(self):
        self.scraper.save_jobs(1, [self.prepare(self.detail())], [])
        path = self.path / "jobs.db"
        with sqlite3.connect(path) as destination:
            self.connection.backup(destination)
        command = [sys.executable, "-m", "Jobs.Locations.locations_func", "--db", str(path),
                   "--gazetteer", str(self.gazetteer)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn("Parsed 1 distinct Workday location strings.", result.stdout)
        result = subprocess.run(command + ["--vendor", "Workday", "--reparse"],
                                capture_output=True, text=True, check=True)
        self.assertIn("Parsed 1 distinct Workday location strings.", result.stdout)
        self.assertNotIn("Greenhouse", result.stdout)


if __name__ == "__main__":
    unittest.main()
