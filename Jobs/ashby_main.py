import json
import requests
from pathlib import Path
from The_Database.db_connect import database_connection_system
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
                "error": str(error),
                "jobs": [],
            }
    
    def data_aquasition(self, list_of_slugs):
        with ThreadPoolExecutor(max_workers=50) as executor:
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
                result.get('status'),
                result.get("job_count", 0),
                result.get("error")
            ))

            for job in result.get("jobs",[]):
                job_rows.append((
                    company_id,
                    job.get("title"),
                    job.get("isRemote"),
                    job.get("descriptionPlain"),
                    job.get("publishedAt"),
                    job.get("employmentType"),
                    job.get("jobUrl"),
                    job.get("compensation",{}).get("compensationTierSummary"),
                    datetime.now().astimezone().isoformat(timespec="seconds")
                ))

        self.database.bulk_insert_scrape_logs(scrape_log_rows)
        self.database.bulk_insert_job_possting(job_rows,"Ashby")


def main():
    ashby_system = Ashby_Algor()
    company_slugs = ashby_system.load_company_slugs()
    ashby_system.data_aquasition(company_slugs)
    
if __name__ == "__main__":
    main()

# python -m Jobs.ashby_main