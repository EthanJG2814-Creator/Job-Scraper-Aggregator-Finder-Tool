import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from website import app


class WebsiteLocationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "jobs.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE companies (company_id INTEGER PRIMARY KEY, slug_name TEXT);
                CREATE TABLE sources (source_id INTEGER PRIMARY KEY, company_id INTEGER, ats_vendor TEXT);
                CREATE TABLE job_postings (
                    job_posting_id INTEGER PRIMARY KEY, company_id INTEGER,
                    job_title TEXT, job_description TEXT, published_date TEXT,
                    last_scanned TEXT, employment_type TEXT, job_remote TEXT,
                    compensation_summary TEXT, job_url TEXT
                );
                CREATE TABLE locations (
                    job_posting_id INTEGER, city_name TEXT, state_abbv TEXT,
                    country TEXT, is_remote INTEGER, is_hybrid INTEGER
                );
                INSERT INTO companies VALUES (1, 'board');
                INSERT INTO sources VALUES (1, 1, 'Greenhouse');
                INSERT INTO job_postings VALUES
                    (1, 1, 'Engineer', 'Python', NULL, datetime('now'), NULL, NULL, NULL, 'url1'),
                    (2, 1, 'Engineer', 'Python', NULL, datetime('now'), NULL, NULL, NULL, 'url2'),
                    (3, 1, 'Analyst', 'SQL', NULL, datetime('now'), NULL, NULL, NULL, 'url3'),
                    (4, 1, 'Unknown', 'Python', NULL, datetime('now'), NULL, NULL, NULL, 'url4'),
                    (5, 1, 'Old', 'Python', NULL, '2000-01-01', NULL, NULL, NULL, 'url5');
                INSERT INTO locations VALUES
                    (1, 'San Francisco', 'CA', 'US', 0, 0),
                    (1, 'New York City', 'NY', 'US', 0, 0),
                    (1, 'San Francisco', 'CA', 'US', 0, 0),
                    (2, 'London', NULL, 'GB', 0, 1),
                    (3, 'Los Angeles', 'CA', 'US', 0, 0),
                    (3, 'London', NULL, 'GB', 0, 0),
                    (5, 'San Francisco', 'CA', 'US', 0, 0);
            """)
        self.patcher = patch.object(app, "DATABASE_PATH", self.db)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def search(self, selection=None, **params):
        values = {name: [value] for name, value in params.items()}
        if selection is not None:
            values["location"] = [json.dumps(selection)]
        return app.search_jobs(values)

    def ids(self, result):
        return {job["id"] for job in result["jobs"]}

    def test_options_hierarchy_and_deduplication(self):
        result = app.location_options()
        self.assertTrue(result["available"])
        countries = {country["code"]: country for country in result["countries"]}
        self.assertEqual(countries["US"]["states"], {
            "CA": ["Los Angeles", "San Francisco"], "NY": ["New York City"]
        })
        self.assertEqual(countries["US"]["state_names"]["CA"], "California")
        self.assertEqual(countries["GB"]["cities"], ["London"])

    def test_country_state_and_city_filters(self):
        self.assertEqual(self.ids(self.search()), {1, 2, 3, 4})
        self.assertEqual(self.ids(self.search([{"country": "US"}])), {1, 3})
        self.assertEqual(self.ids(self.search([{"country": "US", "states": {"NY": []}}])), {1})
        self.assertEqual(self.ids(self.search([{"country": "US", "states": {"CA": []}}])), {1, 3})
        self.assertEqual(self.ids(self.search([{"country": "US", "states": {"CA": ["Los Angeles"]}}])), {3})
        self.assertEqual(self.ids(self.search([{"country": "GB", "cities": ["London"]}])), {2, 3})
        self.assertEqual(self.ids(self.search([{"country": "US", "states": {"NY": []}}, {"country": "GB"}])), {1, 2, 3})
        self.assertEqual(self.ids(self.search([{"country": "US", "states": {"NY": [], "CA": ["Los Angeles"]}}])), {1, 3})
        self.assertEqual(self.ids(self.search([{"country": "US", "states": {"CA": []}}], title="Analyst")), {3})
        self.assertEqual(self.search([{"country": "US"}])["total"], 2)

    def test_matching_city_and_state_must_belong_to_same_row(self):
        result = self.search([{"country": "US", "states": {"CA": ["New York City"]}}])
        self.assertEqual(result["total"], 0)

    def test_locations_returned_once_and_unlocated_jobs_remain_visible(self):
        jobs = {job["id"]: job for job in self.search()["jobs"]}
        self.assertEqual(len(jobs[1]["locations"]), 2)
        self.assertEqual(jobs[4]["locations"], [])
        self.assertEqual(jobs[2]["locations"][0]["is_hybrid"], 1)

    def test_invalid_filters_are_rejected(self):
        for value in ("not json", "[]", json.dumps([{"country": "US", "states": {"CA": "bad"}}]),
                      json.dumps([{"country": "US", "cities": ["x"], "states": {"CA": []}}])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.search_jobs({"location": [value]})

    def test_older_database_without_locations_still_searches(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE locations")
        self.assertFalse(app.location_options()["available"])
        self.assertEqual(self.ids(self.search()), {1, 2, 3, 4})
        with self.assertRaises(sqlite3.Error):
            self.search([{"country": "US"}])

    def test_legacy_location_table_without_work_flags(self):
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                ALTER TABLE locations RENAME TO old_locations;
                CREATE TABLE locations (job_posting_id INTEGER, city_name TEXT, state_abbv TEXT, country TEXT);
                INSERT INTO locations SELECT job_posting_id, city_name, state_abbv, country FROM old_locations;
            """)
        jobs = {job["id"]: job for job in self.search()["jobs"]}
        self.assertEqual(jobs[1]["locations"][0]["is_remote"], 0)


if __name__ == "__main__":
    unittest.main()
