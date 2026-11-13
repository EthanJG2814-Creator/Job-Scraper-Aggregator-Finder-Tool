import contextlib
import io
import subprocess
import sys
import unittest
from unittest.mock import patch

import Main


class MainLocationParserTests(unittest.TestCase):
    def test_icims_chunks_share_one_write_lock(self):
        events = []
        with patch.object(Main, "icims_main_system") as factory, \
             patch.object(Main, "run_icims_chunk", side_effect=lambda chunk, lock: events.append((chunk, lock))):
            factory.return_value.load_company_slugs.return_value = ["a", "b", "c", "d"]
            Main.run_icims_parallel()
        self.assertEqual(len(events), 4)
        self.assertEqual({slug for chunk, _ in events for slug in chunk}, {"a", "b", "c", "d"})
        self.assertEqual(len({id(lock) for _, lock in events}), 1)
        factory.return_value.database.close_database.assert_called_once()

    def test_empty_icims_slugs_are_allowed(self):
        with patch.object(Main, "icims_main_system") as factory, \
             patch.object(Main, "run_icims_chunk") as chunk:
            factory.return_value.load_company_slugs.return_value = []
            Main.run_icims_parallel()
        chunk.assert_not_called()
        factory.return_value.database.close_database.assert_called_once()

    def test_parser_runs_after_all_scrapers(self):
        events = []

        def run(command, **kwargs):
            events.append((command, kwargs))

        with patch.object(Main, "SEQUENTIAL_SCRAPERS", ("Jobs.ashby_main",)), \
             patch.object(Main, "run_icims_parallel", side_effect=lambda: events.append("icims")), \
             patch.object(Main.subprocess, "run", side_effect=run):
            Main.run_scrapers()
        self.assertEqual(events, [
            ([sys.executable, "-u", "-m", "Jobs.ashby_main"], {"cwd": Main.PROJECT_ROOT, "check": True}),
            "icims",
            ([sys.executable, "-u", "-m", "Jobs.workday_alt"], {"cwd": Main.PROJECT_ROOT, "check": True}),
            ([sys.executable, "-u", "-m", "Jobs.Locations.locations_func"],
             {"cwd": Main.PROJECT_ROOT, "check": True}),
        ])

    def test_scraper_failure_prevents_parser(self):
        with patch.object(Main, "SEQUENTIAL_SCRAPERS", ("Jobs.ashby_main",)), \
             patch.object(Main.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "scraper")) as run, \
             patch.object(Main, "run_icims_parallel") as icims:
            with self.assertRaises(subprocess.CalledProcessError):
                Main.run_scrapers()
        self.assertEqual(run.call_count, 1)
        icims.assert_not_called()

    def test_parser_failure_reports_failure(self):
        def run(command, **kwargs):
            if "Jobs.Locations.locations_func" in command:
                raise subprocess.CalledProcessError(2, command)

        stderr = io.StringIO()
        with patch.object(Main, "SEQUENTIAL_SCRAPERS", ()), \
             patch.object(Main, "initialize_database"), \
             patch.object(Main, "run_icims_parallel"), \
             patch.object(Main.subprocess, "run", side_effect=run), \
             patch.object(sys, "argv", ["Main.py"]), \
             contextlib.redirect_stderr(stderr):
            self.assertEqual(Main.main(), 1)
        self.assertIn("location parser failed with exit code 2", stderr.getvalue())

    def test_init_only_does_not_run_parser(self):
        with patch.object(Main, "initialize_database") as initialize, \
             patch.object(Main, "run_scrapers") as scrapers, \
             patch.object(sys, "argv", ["Main.py", "--init-only"]):
            self.assertEqual(Main.main(), 0)
        initialize.assert_called_once()
        scrapers.assert_not_called()


if __name__ == "__main__":
    unittest.main()
