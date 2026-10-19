import json
import requests
from pathlib import Path
from The_Database.db_connect import database_connection_system
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# global variables
PROJECT_ROOT = Path(__file__).resolve().parents[1]
workday_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "workday.json"

class workday_algor():
    def __init__(self):
        self.database = database_connection_system()
        self.list_of_slugs = []
        self.jobs_worked = 0
        self.jobs_failed= 0 
        self.job_429 = 0
        self.job_404 = 0
        self.job_403 = 0
        self.job_500 = 0
        self.job_502 = 0
        self.job_503 = 0
        self.job_504 = 0

    def load_company_slugs(self):
        '''
        While the workday slugs are quite different from the rest of the slugs,
        we will still use the same function to send all the slugs to the database
        '''
        with workday_COMPANIES_PATH.open('r', encoding="utf-8") as companies_file:
            values = json.load(companies_file)

        for value in values:
            slug = value.strip()
            print(slug)
            self.list_of_slugs.append(slug)
            self.database.logging_slugs(slug,"workday")

    def data_aquasition(self):
        '''
        here we are going to try and process multiple workday companies
        to the maximum we can.

        if we send to many request we will start getting 429 errors. 
        '''

        with ThreadPoolExecutor(max_workers=15) as executor:
            results = list(executor.map(self.slug_processing, self.list_of_slugs))
        

    def slug_processing(self,slug):
        print(f'Currently working on slug {slug}')

        try:
            parts = [part.strip() for part in slug.split("|")]

            company, wd, site_id = parts
            wd_num = wd.replace("wd","")

            base_url = f"https://{company}.wd{wd_num}.myworkdayjobs.com"
            api_url = f"{base_url}/wday/cxs/{company}/{site_id}/jobs"


            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "JobScraper/1.0",
                "Origin": base_url,
                "Referer": f"{base_url}/{site_id}",
            }

            offset = 0
            limit = 20

            payload = {
            "appliedFacets": {},
            "limit": 20,
            "offset": 0,
            "searchText": "",
            }
            
            with requests.Session() as session:
                session.headers.update(headers) 

                response = session.post(
                    api_url,
                    json=payload,
                    timeout=30,
                )
                response.raise_for_status()


            # a tracker to see how many links we made contact for testing
            self.jobs_worked += 1

        except requests.RequestException as error:
            # a tracker to see how many links failed for testing
            self.jobs_failed += 1

            if error.response is not None:
                status = error.response.status_code

                if status == 429:
                    print(f"RATE LIMITED | {slug} | HTTP 429")
                    self.job_429 += 1

                elif status == 500:
                    print(f"SERVER ERROR | {slug} | HTTP 500")
                    self.job_500 += 1

                elif status == 502:
                    print(f"BAD GATEWAY | {slug} | HTTP 502")
                    self.job_502 += 1

                elif status == 503:
                    print(f"SERVICE UNAVAILABLE | {slug} | HTTP 503")
                    self.job_503 += 1

                elif status == 504:
                    print(f"GATEWAY TIMEOUT | {slug} | HTTP 504")
                    self.job_504 += 1

                elif status == 403:
                    print(f"FORBIDDEN | {slug} | HTTP 403")
                    self.job_403 += 1

                elif status == 404:
                    print(f"NOT FOUND | {slug} | HTTP 404")
                    self.job_404 += 1

                else:
                    print(f"HTTP ERROR | {slug} | HTTP {status} | {error}")

            elif isinstance(error, requests.Timeout):
                print(f"TIMEOUT | {slug} | {error}")

            elif isinstance(error, requests.ConnectionError):
                print(f"CONNECTION ERROR | {slug} | {error}")

            else:
                print(f"REQUEST ERROR | {slug} | {error}")
            

    def status(self):
        print(f"This is how many companies we contacted:{self.jobs_worked} ")
        print(f'This is how many companies failed: {self.jobs_failed}')
        print(f'This is the total number of 429 errors: {self.job_429}')
        print(f'This is the total number of 500 errors: {self.job_500}')
        print(f'This is the total number of 502 errors: {self.job_502}')
        print(f'This is the total number of 503 errors: {self.job_503}')
        print(f'This is the total number of 504 errors: {self.job_504}')
        print(f'This is the total number of 403 errors: {self.job_403}')
        print(f'This is the total number of 404 errors: {self.job_404}')
  

        #print(f'this is the total number of jobs we found from lever: {self.total_jobs_found}')


def main():
    workday_system = workday_algor()
    workday_system.load_company_slugs()
    workday_system.data_aquasition()
    workday_system.status()

if __name__ == "__main__":
    main()

# python -m Jobs.workday_main
