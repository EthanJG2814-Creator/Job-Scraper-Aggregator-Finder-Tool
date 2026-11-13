import unittest
from unittest.mock import patch

import requests

from Jobs.icims_main import icims_main_system


class ICIMSSlugTests(unittest.TestCase):
    def test_sitemap_urls_use_unprefixed_company_slug(self):
        scraper = icims_main_system.__new__(icims_main_system)
        cases = {
            "instamed": "instamed",
            "careers-instamed": "instamed",
            "jobs-instamed": "instamed",
            "job-instamed": "instamed",
            "careers-internova": "internova",
            "my-careers-company": "my-careers-company",
            "careers-jobs-company": "jobs-company",
        }
        for slug, company in cases.items():
            with self.subTest(slug=slug):
                job = {"id": "42", "title": "Engineer"}
                with patch.object(scraper, "_fetch_sitemap_jobs", side_effect=[[job], []]) as fetch:
                    result = scraper.slug_processing(slug)

                self.assertEqual(
                    [call.args[0] for call in fetch.call_args_list],
                    [
                        f"https://careers-{company}.icims.com/sitemap.xml",
                        f"https://jobs-{company}.icims.com/sitemap.xml",
                    ],
                )
                self.assertEqual(result["company_slug"], slug)
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["job_count"], 1)
                self.assertEqual(result["jobs"], [job])

    def test_jobs_sitemap_still_used_when_careers_fails(self):
        scraper = icims_main_system.__new__(icims_main_system)
        job = {"id": "42", "title": "Engineer"}
        with patch.object(
            scraper, "_fetch_sitemap_jobs",
            side_effect=[requests.HTTPError("404"), [job]],
        ) as fetch:
            result = scraper.slug_processing("jobs-instamed")

        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            [
                "https://careers-instamed.icims.com/sitemap.xml",
                "https://jobs-instamed.icims.com/sitemap.xml",
            ],
        )
        self.assertEqual(result["company_slug"], "jobs-instamed")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["error"], None)
        self.assertEqual(result["jobs"], [job])


if __name__ == "__main__":
    unittest.main()
