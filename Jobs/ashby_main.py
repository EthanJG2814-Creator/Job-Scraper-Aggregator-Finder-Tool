import json
import requests
from logging_errors import error_code
from pathlib import Path
from The_Database.db_connect import database_connection_system
from Jobs.Locations.locations_func import ashby_raw_locations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# global variables
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASHBY_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "ashby.json"


class Ashby_Algor():
    def __init__(self):
        self.database = database_connection_system()

    def load_company_slugs(self):
        with ASHBY_COMPANIES_PATH.open('r',  encoding="utf-8") as companies_file:
            values = json.load(companies_file)

        list_of_slugs = []

        for value in values:
            slug = value.strip()
            list_of_slugs.append(slug)
            self.database.logging_slugs(slug,"Ashby")

        return list_of_slugs 

    def slug_processing(self, slug):
        print(f'working on {slug}')

        url = (
        f"https://api.ashbyhq.com/posting-api/job-board/"
        f"{slug}?includeCompensation=true"
        )

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            data = response.json()
            jobs = data.get("jobs", [])

            return {
                "company_slug": slug,
                "ats_vendor": 'Ashby',
                "status": "success",
                "job_count": len(jobs),
                "error": None,
                "jobs": jobs,                
            }

        except requests.RequestException as error:
            return {
               "company_slug": slug,
                "ats_vendor": 'Ashby',
                "status": "fail",
                "job_count": 0,
                "error": error_code(error),
                "jobs": [],
            }
    
    def data_aquasition(self, list_of_slugs):
        with ThreadPoolExecutor(max_workers=50) as executor:
            results = list(executor.map(self.slug_processing, list_of_slugs))

        company_slug_map = self.database.get_company_slug_map()
        scrape_log_rows = []
        job_rows = []
        location_rows = []
        location_contexts = []
        incomplete_location_urls = set()
        workplace_types = {}

        for result in results:
            slug = result.get("company_slug")
            company_id = company_slug_map.get(slug)
            if company_id is None:
                error = ValueError(f"Missing company_id for slug: {slug}")
                self.database.log_errors.location_error(
                    error, "Ashby company lookup", "Ashby", [{"company": slug}]
                )
                raise error

            scrape_log_rows.append((
                company_id,
                result.get("ats_vendor"),
                result.get('status'),
                result.get("job_count", 0),
                result.get("error")
            ))

            for job in result.get("jobs",[]):
                url = job.get("jobUrl")
                vendor_job_id = job.get("id")
                context = {
                    "company": slug, "company_id": company_id,
                    "vendor_job_id": vendor_job_id, "url": url,
                    "location": job.get("location"), "secondaryLocations": job.get("secondaryLocations"),
                    "workplaceType": job.get("workplaceType"),
                }
                if not isinstance(url, str) or not url.strip():
                    self.database.log_errors.location_error(
                        ValueError("Ashby job is missing its job URL"),
                        "Ashby job preparation", "Ashby", [context]
                    )
                    continue
                if vendor_job_id is None:
                    # The documented posting response does not guarantee an ID.
                    vendor_job_id = url
                raw_locations, issues = ashby_raw_locations(job)
                if issues:
                    self.database.log_errors.location_error(
                        ValueError("; ".join(issues)), "Ashby incomplete location", "Ashby", [context]
                    )
                    incomplete_location_urls.add(url)
                location_contexts.append(context)
                location_rows.append((url, str(vendor_job_id), raw_locations))
                workplace = job.get("workplaceType")
                if workplace in ("Remote", "Hybrid", "OnSite"):
                    workplace_types[url] = workplace
                job_rows.append((
                    company_id,
                    job.get("title"),
                    job.get("isRemote"),
                    job.get("descriptionPlain"),
                    job.get("publishedAt"),
                    job.get("employmentType"),
                    url,
                    (job.get("compensation") or {}).get("compensationTierSummary"),
                    datetime.now().astimezone().isoformat(timespec="seconds")
                ))

        self.database.bulk_insert_scrape_logs(scrape_log_rows)
        self.database.bulk_insert_jobs_with_locations(
            job_rows, location_rows, "Ashby", location_contexts,
            incomplete_location_urls, workplace_types,
        )


def main():
    ashby_system = Ashby_Algor()
    company_slugs = ashby_system.load_company_slugs()
    ashby_system.data_aquasition(company_slugs)
    
if __name__ == "__main__":
    main()

# python -m Jobs.ashby_main