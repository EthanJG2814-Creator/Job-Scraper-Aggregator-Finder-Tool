import json
import requests
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from email.utils import parsedate_to_datetime
from The_Database.db_connect import database_connection_system

PROJECT_ROOT = Path(__file__).resolve().parents[1]
workday_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "workday.json"


class workday_algor:
    def __init__(self):
        self.database = database_connection_system()
        self.list_of_slugs = []
        self.jobs_worked = 0
        self.jobs_failed = 0
        self.request_lock = Lock()
        self.request_interval = 0.5
        self.next_request_at = 0.0
        self.cooldown_until = 0.0
        self.successful_requests = 0
        self.last_adjustment_at = time.monotonic()

    def load_company_slugs(self):
        with workday_COMPANIES_PATH.open("r", encoding="utf-8") as file:
            values = json.load(file)

        for value in values:
            slug = value.strip()
            print(slug)
            self.list_of_slugs.append(slug)
            self.database.logging_slugs(slug, "workday")

    def data_aquasition(self):
        with ThreadPoolExecutor(max_workers=50) as executor:
            results = list(executor.map(
                self.slug_processing, self.list_of_slugs
            ))

        company_slug_map = self.database.get_company_slug_map()
        scrape_logs_rows = []

        for result in results:
            scrape_logs_rows.append((
                company_slug_map.get(result["company_slug"]),
                result["ats_vendor"],
                result["status"],
                result["job_count"],
                result["error"],
            ))

        self.database.bulk_insert_scrape_logs(scrape_logs_rows)

    def slug_processing(self, slug):
        print(f"Currently working on slug {slug}")

        jobs = []
        seen_paths = set()
        total = None
        error_type = None

        try:
            company, wd, site_id = [
                part.strip() for part in slug.split("|")
            ]

            base_url = f"https://{company}.{wd}.myworkdayjobs.com"
            site_api = f"{base_url}/wday/cxs/{company}/{site_id}"
            api_url = f"{site_api}/jobs"

            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "JobScraper/1.0",
                "Origin": base_url,
                "Referer": f"{base_url}/{site_id}",
            }

            offset = 0
            limit = 20

            with requests.Session() as session:
                session.headers.update(headers)

                while True:
                    data = self.request_workday_page(
                        session,
                        api_url,
                        payload={
                            "appliedFacets": {},
                            "limit": limit,
                            "offset": offset,
                            "searchText": "",
                        },
                    )

                    page_jobs = data["jobPostings"]

                    if total is None:
                        total = data["total"]

                    if not page_jobs:
                        if len(jobs) < total:
                            raise ValueError(
                                "Pagination ended before all jobs were collected."
                            )
                        break

                    new_jobs = 0

                    for job in page_jobs:
                        path = job["externalPath"]

                        if path not in seen_paths:
                            seen_paths.add(path)
                            jobs.append({
                                **job,
                                "job_url": f"{base_url}/{site_id}{path}",
                                "detail_api_url": f"{site_api}{path}",
                            })
                            new_jobs += 1

                    if new_jobs == 0:
                        raise ValueError("Pagination returned a repeated page.")

                    if len(jobs) >= total:
                        break

                    offset += len(page_jobs)

        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            if (
                isinstance(error, requests.RequestException)
                and error.response is not None
            ):
                error_type = str(error.response.status_code)
            else:
                error_type = type(error).__name__

        return {
            "company_slug": slug,
            "ats_vendor": "Workday",
            "status": (
                "success" if error_type is None
                else "partial" if jobs else "fail"
            ),
            "job_count": len(jobs),
            "error": error_type,
            "jobs": jobs,
        }

    def wait_for_request(self):
        while True:
            with self.request_lock:
                now = time.monotonic()
                wait_seconds = self.next_request_at - now

                if wait_seconds <= 0:
                    self.next_request_at = now + self.request_interval
                    return now

            time.sleep(wait_seconds)

    def request_workday_page(self, session, api_url, payload):
        for attempt in range(3):
            started_at = self.wait_for_request()

            with session.post(api_url, json=payload, timeout=30) as response:
                if response.status_code != 429:
                    response.raise_for_status()
                    data = response.json()

                    with self.request_lock:
                        now = time.monotonic()

                        if started_at >= self.last_adjustment_at:
                            self.successful_requests += 1

                            if (
                                self.successful_requests >= 20
                                and now - self.last_adjustment_at >= 30
                            ):
                                self.request_interval = max(
                                    0.05, self.request_interval * 0.9
                                )
                                self.successful_requests = 0
                                self.last_adjustment_at = now
                                print(f'speeding up program: {self.request_interval}')

                    return data

                wait_seconds = 30 * (2 ** attempt)
                retry_after = response.headers.get("Retry-After")

                if retry_after:
                    try:
                        wait_seconds = max(0, int(retry_after))
                    except ValueError:
                        try:
                            retry_at = parsedate_to_datetime(retry_after)

                            if retry_at.tzinfo is None:
                                retry_at = retry_at.replace(tzinfo=timezone.utc)

                            wait_seconds = max(
                                0,
                                (
                                    retry_at - datetime.now(timezone.utc)
                                ).total_seconds(),
                            )
                        except (TypeError, ValueError, OverflowError):
                            pass

                with self.request_lock:
                    now = time.monotonic()

                    if started_at >= self.cooldown_until:
                        self.request_interval = min(
                            10.0, self.request_interval * 5
                        )
                        print(f'slowing down program: {self.request_interval}')

                    self.successful_requests = 0
                    self.last_adjustment_at = now
                    self.cooldown_until = max(
                        self.cooldown_until, now + wait_seconds
                    )
                    self.next_request_at = max(
                        self.next_request_at, self.cooldown_until
                    )

                if attempt == 2:
                    response.raise_for_status()


def main():
    workday_system = workday_algor()
    workday_system.load_company_slugs()
    workday_system.data_aquasition()


if __name__ == "__main__":
    main()