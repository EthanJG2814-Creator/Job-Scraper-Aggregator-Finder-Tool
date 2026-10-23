import json
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, parse_qs, urlsplit, urlunsplit, parse_qsl, urlencode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from logging_errors import Error_Log_system, error_code

from The_Database.db_connect import database_connection_system

# color code
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_ORANGE = "\033[38;5;208m"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ICIMS_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "icims.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/144.0.0.0 Safari/537.36"
    )
}


class icims_main_system:
    def __init__(self):
        self.database = database_connection_system()
        self.logging_errors = Error_Log_system()


    def load_company_slugs(self):
        with ICIMS_COMPANIES_PATH.open("r", encoding="utf-8") as companies_file:
            values = json.load(companies_file)

        list_of_slugs = []

        for value in values:
            slug = value.strip()
            list_of_slugs.append(slug)
            self.database.logging_slugs(slug,"ICIMS")

        return list_of_slugs

    def _fetch_job_description(self, job_url, session):
        try:
            # Preserve existing query parameters, but set in_iframe=1.
            url_parts = urlsplit(job_url)
            query = dict(parse_qsl(url_parts.query, keep_blank_values=True))
            query["in_iframe"] = "1"

            detail_url = urlunsplit(
                url_parts._replace(query=urlencode(query))
            )

            response = session.get(detail_url, timeout=20)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            content = soup.select_one(".iCIMS_JobContent")

            if content is None or "":
                self.logging_errors.icims_error_catching("The description is none!", job_url)
                return "N/a"

            # Collect the section headings and their description bodies.
            sections = content.select(
                "h2.iCIMS_InfoField_Job, "
                "div.iCIMS_InfoMsg_Job"
            )

            if not sections:
                self.logging_errors.icims_error_catching("found job content, but no description", job_url)

            description_parts = []

            for section in sections:
                text = section.get_text(separator=" ", strip=True)

                if text:
                    description_parts.append(text)

            description = "\n\n".join(description_parts)

            if description is "":
                self.logging_errors.icims_error_catching("empty string snuck by the system", job_url)

            return description

        except requests.RequestException as error:
            self.logging_errors.icims_error_catching(error,job_url)
            return "N/A"

    def _fetch_sitemap_urls(self, sitemap_url, session, visited=None):
        if visited is None:
            visited = set()

        if sitemap_url in visited:
            return []

        visited.add(sitemap_url)

        response = session.get(sitemap_url, timeout=20)
        response.raise_for_status()

        root = ET.fromstring(response.content)
        root_type = root.tag.rsplit("}", 1)[-1]
        urls = []

        if root_type == "sitemapindex":
            for node in root.findall("{*}sitemap/{*}loc"):
                if node.text:
                    urls.extend(
                        self._fetch_sitemap_urls(
                            urljoin(response.url, node.text.strip()),
                            session,
                            visited,
                        )
                    )

        elif root_type == "urlset":
            for node in root.findall("{*}url/{*}loc"):
                if node.text:
                    urls.append(urljoin(response.url, node.text.strip()))

        else:
            raise ValueError("Response was not XML sitemap content")

        return urls

    def _fetch_sitemap_jobs(self, sitemap_url, session):
        job_urls = self._fetch_sitemap_urls(sitemap_url, session)

        '''
        IF A WEBSITE EXIST IT WILL PUL LTHE CAREERS PAGE AND EACH JOB POSTING URL
        FOR EXAMPLE:

        [CAREERS URL, JOB POSTING_1, POSTING 2, ...]
        '''

        jobs = []
        seen_job_ids = set()

        for job_url in job_urls:
            parts = urlsplit(job_url).path.strip("/").split("/")

            if (len(parts) < 3 or parts[0].lower() != "jobs" or not parts[1].isdigit() or not parts[2]):
                continue
            job_id = parts[1]

            if job_id in seen_job_ids:
                continue

            title = (
                unquote(parts[2])
                .replace("-", " ")
                .replace("+", " ")
                .strip()
                .title()
            )

            if not title:
                continue

            seen_job_ids.add(job_id)

            jobs.append(
                {
                    "id": job_id,
                    "title": title,
                    "remote": False,
                    "description": self._fetch_job_description(job_url, session),
                    "url": job_url,
                }
            )

        return jobs

    def slug_processing(self, slug):
        print(f"working on {slug}")

        sitemap_urls = [
            f"https://careers-{slug}.icims.com/sitemap.xml",
            f"https://jobs-{slug}.icims.com/sitemap.xml",]

        jobs = []
        errors = []
        completed_sitemaps = 0

        with requests.Session() as session:
            session.headers.update(HEADERS)

            for sitemap_url in sitemap_urls:
                try:
                    jobs = self._fetch_sitemap_jobs(sitemap_url, session)
                    completed_sitemaps += 1

                except (requests.RequestException, ET.ParseError, ValueError) as error:
                    errors.append(error_code(error))

        return {
            "company_slug": slug,
            "ats_vendor": "iCIMS",
            "status": (
                "success" if jobs else "empty" if completed_sitemaps else "fail"
            ),
            "job_count": len(jobs),
            "error": None if jobs else ",".join(dict.fromkeys(errors)) or None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "jobs": jobs,
        }

    def data_aquasition(self, list_of_slugs):
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(self.slug_processing, list_of_slugs))

        company_slug_map = self.database.get_company_slug_map()
        scrape_log_rows = []
        job_rows = []

        for result in results:
            slug = result.get("company_slug")
            company_id = company_slug_map.get(slug)

            scrape_log_rows.append(
                (
                    company_id,
                    result.get("ats_vendor"),
                    result.get("status"),
                    result.get("job_count", 0),
                    result.get("error"),
                )
            )

            for job in result.get("jobs",[]):
                job_rows.append(
                    (
                        company_id,
                        job.get("title"),
                        job.get("remote", False),
                        job.get("description"),
                        None,
                        None,
                        job.get("url"),
                        None,
                        datetime.now().astimezone().isoformat(timespec="seconds")
                    )   

                )

        self.database.bulk_insert_scrape_logs(scrape_log_rows)
        self.database.bulk_insert_job_possting(job_rows, "ICIMS")


def main():
    icims_system = icims_main_system()
    company_slugs = icims_system.load_company_slugs()
    icims_system.data_aquasition(company_slugs)


if __name__ == "__main__":
    main()


# python -m Jobs.icims_main