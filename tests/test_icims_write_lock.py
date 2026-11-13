import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from Jobs.icims_main import icims_main_system


class ICIMSWriteLockTests(unittest.TestCase):
    def test_concurrent_batches_wait_for_same_process_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE saved (url TEXT)")
            writer_started = threading.Event()
            writer_lock = threading.Lock()

            class Database:
                def __init__(self):
                    self.connection = sqlite3.connect(path, timeout=0.05)

                def bulk_insert_jobs_with_locations(self, jobs, *args):
                    self.connection.execute("BEGIN IMMEDIATE")
                    writer_started.set()
                    time.sleep(0.2)
                    self.connection.executemany("INSERT INTO saved VALUES (?)", [(job[6],) for job in jobs])
                    self.connection.commit()

                def bulk_insert_scrape_logs(self, rows):
                    pass

            def save(identifier):
                scraper = icims_main_system.__new__(icims_main_system)
                scraper.database = Database()
                scraper.write_lock = writer_lock
                try:
                    result = {"company_slug": "board", "ats_vendor": "iCIMS", "status": "success",
                              "job_count": 1, "error": None,
                              "jobs": [{"id": str(identifier), "url": f"https://example.test/{identifier}",
                                        "title": "Engineer", "description": "Description", "locations": [],
                                        "location_issues": []}]}
                    scraper._save_results([result], {"board": 1})
                finally:
                    scraper.database.connection.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(save, 1)
                self.assertTrue(writer_started.wait(2))
                second = executor.submit(save, 2)
                first.result()
                second.result()
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM saved").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
