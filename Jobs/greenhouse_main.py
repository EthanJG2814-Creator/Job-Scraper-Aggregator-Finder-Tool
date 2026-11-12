import json
import requests
from logging_errors import error_code
from pathlib import Path
from The_Database.db_connect import database_connection_system
from Jobs.Locations.locations_func import greenhouse_raw_locations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from bs4 import BeautifulSoup
import html

# global variables
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASHBY_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "greenhouse_comp.json"

class greenhouse_main_system():
    def __init__(self):
        self.database = database_connection_system()

    def load_company_slugs(self):
        with ASHBY_COMPANIES_PATH.open('r',  encoding="utf-8") as companies_file:
            values = json.load(companies_file)

        list_of_slugs = []

        for value in values:
            slug = value.strip()
            list_of_slugs.append(slug)
            self.database.logging_slugs(slug,"Greenhouse")

        return list_of_slugs 

    def slug_processing(self, slug):
            print(f"working on {slug}")

            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()

                data = response.json()
                jobs = data.get("jobs", [])

                return {
                    "company_slug": slug,
                    "ats_vendor": "Greenhouse",
                    "status": "success",
                    "job_count": len(jobs),
                    "error": None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "jobs": jobs,
                }

            except requests.RequestException as error:
                self.database.log_errors.location_error(
                    error, "Greenhouse request", "Greenhouse",
                    [{"company": slug, "vendor_job_id": None, "location": None, "url": url}],
                )
                return {
                    "company_slug": slug,
                    "ats_vendor": "Greenhouse",
                    "status": "fail",
                    "job_count": 0,
                    "error": error_code(error),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "jobs": [],
                }

    def data_aquasition(self, list_of_slugs):
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(self.slug_processing, list_of_slugs))

        company_slug_map = self.database.get_company_slug_map()
        scrape_log_rows = []
        job_rows = []
        location_rows = []
        location_contexts = []
        incomplete_location_urls = set()

        for result in results:
            slug = result.get("company_slug")
            company_id = company_slug_map.get(slug)

            if company_id is None:
                error = ValueError(f"Missing company_id for slug: {slug}")
                self.database.log_errors.location_error(
                    error, "Greenhouse company lookup", "Greenhouse",
                    [{"company": slug, "company_id": None, "vendor_job_id": None,
                      "location": None, "url": None}],
                )
                raise error

            scrape_log_rows.append((
                company_id,
                result.get("ats_vendor"),
                result.get("status"),
                result.get("job_count", 0),
                result.get("error"),
            ))

            for job in result.get("jobs",[]):
                url = job.get("absolute_url")
                vendor_job_id = job.get("id")
                context = {
                    "company": slug, "company_id": company_id,
                    "vendor_job_id": vendor_job_id, "url": url,
                    "location": job.get("location"), "locations": job.get("locations"),
                }
                try:
                    if not url or vendor_job_id is None:
                        raise ValueError("Greenhouse job is missing its URL or job ID")
                    raw_locations, location_issues = greenhouse_raw_locations(job)
                    job_row = (
                        company_id,
                        job.get("title"),
                        "N/A",
                        self.clean_html(job.get("content")),
                        job.get("first_published"),
                        self.get_employment_type(job),
                        url,
                        self.get_pay_range(job),
                        datetime.now().astimezone().isoformat(timespec="seconds")
                    )
                except Exception as error:
                    self.database.log_errors.location_error(
                        error, "Greenhouse job preparation", "Greenhouse", [context]
                    )
                    raise
                if location_issues:
                    self.database.log_errors.location_error(
                        ValueError("; ".join(location_issues)),
                        "Greenhouse incomplete location", "Greenhouse", [context],
                    )
                    incomplete_location_urls.add(url)
                location_contexts.append(context)
                location_rows.append((url, str(vendor_job_id), raw_locations))
                job_rows.append(job_row)

        try:
            self.database.bulk_insert_scrape_logs(scrape_log_rows)
        except Exception as error:
            self.database.log_errors.location_error(
                error, "Greenhouse scrape-log save (candidate jobs)", "Greenhouse", location_contexts
            )
            raise
        self.database.bulk_insert_jobs_with_locations(
            job_rows, location_rows, "Greenhouse", location_contexts, incomplete_location_urls
        )

    def clean_html(self,html_text):
        if not html_text:
            return None
        decoded_html = (html.unescape(html_text))
        soup = BeautifulSoup(decoded_html, "html.parser")
        return soup.get_text()

    def get_employment_type(self, job):
        for field in job.get("metadata") or []:
            if field.get("name", "").strip().lower() == "employment type":
                return field.get("value") or "N/A"

        return "N/A"

    def get_pay_range(self,job):
        pay_ranges = job.get("pay_input_ranges", [])

        if not pay_ranges:
            return "N/A"

        pay = pay_ranges[0]

        min_cents = pay.get("min_cents")
        max_cents = pay.get("max_cents")
        currency_type = pay.get("currency_type", "N/A")

        min_amount = f"{min_cents // 100:,}" if min_cents is not None else None
        max_amount = f"{max_cents // 100:,}" if max_cents is not None else None

        if min_amount and max_amount:
            return f"{min_amount} - {max_amount} {currency_type}"
        if min_amount:
            return f"{min_amount} {currency_type}"
        if max_amount:
            return f"{max_amount} {currency_type}"

        return f"N/A {currency_type}" if currency_type != "N/A" else "N/A"


def main():
    greenhouse_algor = greenhouse_main_system()
    company_slugs = greenhouse_algor.load_company_slugs()
    greenhouse_algor.data_aquasition(company_slugs)


if __name__ == "__main__":
    main()

# python -m Jobs.greenhouse_main