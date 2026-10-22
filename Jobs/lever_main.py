import json 
import requests
from pathlib import Path
from The_Database.db_connect import database_connection_system
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# global variables
# global variables
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEVER_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "lever.json"


class lever_algor():
    def __init__(self):
        self.database = database_connection_system()
        self.jobs_worked = 0
        self.jobs_failed= 0 
        self.list_of_slugs = []
        self.total_jobs_found = 0

    def load_company_slugs(self):
            with LEVER_COMPANIES_PATH.open('r',  encoding="utf-8") as companies_file:
                values = json.load(companies_file)

            for value in values:
                slug = value.strip()
                self.list_of_slugs.append(slug)
                self.database.logging_slugs(slug,"Lever")

            return self.list_of_slugs 

    def data_aquasition(self):
        '''
        here is where we process all the slugs and exatract
        the company inforomation from each request
        '''
         
        with ThreadPoolExecutor(max_workers=30) as executor:
            results = list(executor.map(self.slug_processing, self.list_of_slugs))

        company_slug_map = self.database.get_company_slug_map()
        scrape_logs_rows = [] 
        job_rows = []

        for result in results:
            slug = result.get("company_slug")
            company_id = company_slug_map.get(slug)

            scrape_logs_rows.append((
                company_id,
                result.get("ats_vendor"),
                result.get('status'),
                result.get("job_count", 0),
                result.get("error")
            ))

            for job in result.get("jobs",[]):

                job_salary = self.get_salary(job)

                job_rows.append((
                    company_id,
                    job.get("text"), # this is the job title
                    "N/A", # this is to confirm if the job posting is Remote or note
                    job.get("descriptionPlain"), # This is to get the job description
                    self.get_original_published_date(job.get("createdAt")), # this is to get the original date this job posting was published
                    job.get("commitment"), # this is to get the type of employment full time or part time, sometimes remote
                    job.get("applyUrl"), # this is the job posting url
                    job_salary, # this is the salary range
                    datetime.now().astimezone().isoformat(timespec="seconds") # this will update everytimne we ping a particular job posting
                ))

        self.database.bulk_insert_scrape_logs(scrape_logs_rows)
        self.database.bulk_insert_job_possting(job_rows,"Lever")


    def slug_processing(self,slug):
        print(f'Currently working on slug {slug}')

        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"

        try:
            response = requests.get(url,timeout=30)
            response.raise_for_status()
            jobs = response.json()

            self.jobs_worked += 1
            self.total_jobs_found += len(jobs)

            return {
                "company_slug": slug,
                "ats_vendor":'Lever',
                "status":"success",
                "job_count": len(jobs),
                "error": None,
                "jobs":jobs,
            }
            
        except requests.RequestException as error:
            self.jobs_failed += 1
            return{
                "company_slug": slug,
                "ats_vendor": 'Lever',
                "status": "fail",
                "job_count":0,
                "error":str(error),
                "jobs":[],
            }

    def get_salary(self,job):
        '''
        this is to extract the salary range from the job posting
        '''

        salary = job.get("salaryRange") or {}

        salary_min = salary.get("min")
        salary_max = salary.get("max")
        currency = salary.get("currency") or ""

        if salary_min is not None and salary_max is not None:
            salary_range = f"{salary_min:,.0f} - {salary_max:,.0f} {currency}".strip()
        elif salary_min is not None:
            salary_range = f"From {salary_min:,.0f} {currency}".strip()
        elif salary_max is not None:
            salary_range = f"Up to {salary_max:,.0f} {currency}".strip()
        else:
            salary_range = None

        return salary_range

    def get_original_published_date(self,time_stamp):
        creation_date = datetime.fromtimestamp(
            time_stamp / 1000,
            tz = timezone.utc,
        )
        return creation_date.isoformat()

    def status(self):
        print(f"This is how many companies we contacted:{self.jobs_worked} ")
        print(f'This is how many companies failed: {self.jobs_failed}')
        print(f'this is the total number of jobs we found from lever: {self.total_jobs_found}')

def main():
    lever_system = lever_algor()
    lever_system.load_company_slugs()
    lever_system.data_aquasition()
    lever_system.status()

if __name__ == "__main__":
    main()

# python -m Jobs.lever_main