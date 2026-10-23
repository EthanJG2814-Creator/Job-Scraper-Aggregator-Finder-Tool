import json
import requests
from logging_errors import error_code
from pathlib import Path
from The_Database.db_connect import database_connection_system
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from bs4 import BeautifulSoup


# color code
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_ORANGE = "\033[38;5;208m"


# global variables
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BAMBOOHR_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "bamboohr.json"


class bamboohr_main_system():
    def __init__(self):
        self.database = database_connection_system()


    def load_company_slugs(self):
        with BAMBOOHR_COMPANIES_PATH.open('r', encoding="utf-8") as companies_file:
            values = json.load(companies_file)

        list_of_slugs = []

        for value in values:
            slug = value.strip()
            list_of_slugs.append(slug)
            self.database.logging_slugs(slug,"bamboohr")

        return list_of_slugs


    def fetch_job_detail(self, slug, job_id):
        url = f"https://{slug}.bamboohr.com/careers/{job_id}/detail"

        response = requests.get(
            url,
            headers={"Accept": "application/json"},
            timeout=30
        )
        response.raise_for_status()

        data = response.json()
        return data.get("result", {}).get("jobOpening", {})


    def slug_processing(self, slug):
        print(f"working on {slug}")

        url = f"https://{slug}.bamboohr.com/careers/list"

        try:
            response = requests.get(
                url,
                headers={"Accept": "application/json"},
                timeout=30
            )
            response.raise_for_status()

            data = response.json()
            listings = data.get("result", [])
            jobs = []

            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_job_id = {
                    executor.submit(self.fetch_job_detail, slug, job.get("id")): job.get("id")
                    for job in listings
                    if job.get("id")
                }

                for future in as_completed(future_to_job_id):
                    job_id = future_to_job_id[future]

                    try:
                        opening = future.result()
                        jobs.append(opening)
                    except requests.RequestException as error:
                        print(f"DETAIL FAILED | {slug} | {job_id} | {error}")

            return {
                "company_slug": slug,
                "ats_vendor": "BambooHR",
                "status": "success",
                "job_count": len(listings),
                "error": None,
                "jobs": jobs,
            }

        except requests.RequestException as error:
            return {
                "company_slug": slug,
                "ats_vendor": "BambooHR",
                "status": "fail",
                "job_count": 0,
                "error": error_code(error),
                "jobs": [],
            }


    def data_aquasition(self, list_of_slugs):
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(self.slug_processing, list_of_slugs))

        company_slug_map = self.database.get_company_slug_map()
        scrape_log_rows = []
        job_rows = []

        for result in results:
            slug = result.get("company_slug")
            company_id = company_slug_map.get(slug)

            scrape_log_rows.append((
                company_id,
                result.get("ats_vendor"),
                result.get("status"),
                result.get("job_count", 0),
                result.get("error"),
            ))

            for job in result.get("jobs", []):
                location = job.get("location", {})
                location_text = self.get_location_text(location)

                job_rows.append((
                    company_id,
                    job.get("jobOpeningName"),
                    self.get_remote_status(job),
                    self.clean_html(job.get("description")),
                    job.get("datePosted"),
                    job.get("employmentStatusLabel"),
                    job.get("jobOpeningShareUrl"),
                    job.get("compensation", "N/A"),
                    datetime.now().astimezone().isoformat(timespec="seconds")
                ))

        self.database.bulk_insert_scrape_logs(scrape_log_rows)
        self.database.bulk_insert_job_possting(job_rows,"Bamboohr")


    def clean_html(self, html_text):
        if not html_text:
            return None

        soup = BeautifulSoup(html_text, "html.parser")
        return soup.get_text(" ", strip=True)


    def get_remote_status(self, job):
        location = job.get("location", {})
        location_values = " ".join(str(value) for value in location.values()).lower()

        if "remote" in location_values or job.get("locationType") == 1:
            return True

        if job.get("locationType") == 2:
            return False

        return "N/A"


    def get_location_text(self, location):
        if not isinstance(location, dict):
            return "N/A"

        city = location.get("city")
        state = location.get("state")
        country = location.get("addressCountry")

        parts = [part for part in [city, state, country] if part]
        return ", ".join(parts) if parts else "N/A"


def main():
    bamboohr_system = bamboohr_main_system()
    company_slugs = bamboohr_system.load_company_slugs()
    bamboohr_system.data_aquasition(company_slugs)


if __name__ == "__main__":
    main()


# python -m Jobs.bamboohr_main