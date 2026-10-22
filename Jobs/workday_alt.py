import json
import re
import requests
from bs4 import BeautifulSoup
from Jobs import workday_main
from Jobs.workday_main import workday_algor, RateLimiter

# Job pages are a different route from the /wday/cxs API, so they get their own
# limiter: a 429 on one route never freezes the other.
PAGE_START_INTERVAL = 0.1

JSON_LD_PATTERN = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
EMPLOYMENT_LABELS = {
    "FULL_TIME": "Full time", "PART_TIME": "Part time", "CONTRACTOR": "Contractor",
    "TEMPORARY": "Temporary", "INTERN": "Intern", "VOLUNTEER": "Volunteer",
    "PER_DIEM": "Per diem", "OTHER": "Other",
}


class workday_alt_algor(workday_algor):
    """Same as workday_algor, but job details come from the public job page's
    JSON-LD block instead of the detail API. Falls back to the API when a page
    has no JSON-LD (for example an expired job)."""

    def __init__(self):
        super().__init__()
        self.page_limiter = RateLimiter("job pages")
        self.page_limiter.interval = PAGE_START_INTERVAL
        self.page_hits = self.api_fallbacks = 0

    def get_job_detail(self, detail_url, job_url, summary, headers, ctx):
        try:
            job = self.get_job_from_page(job_url, summary, headers, ctx)
            self.page_hits += 1
            return job
        except (requests.RequestException, ValueError, KeyError, TypeError):
            self.api_fallbacks += 1
            return super().get_job_detail(detail_url, job_url, summary, headers, ctx)

    def get_job_from_page(self, job_url, summary, headers, ctx):
        html = self.request_job_page(job_url, headers, ctx)
        data = self.parse_job_posting(html)
        title = data.get("title") or summary.get("title")
        description = BeautifulSoup(
            data.get("description") or "", "html.parser").get_text(" ", strip=True)
        if not title or not description:
            raise ValueError("Job page is missing title or description.")
        employment_type = EMPLOYMENT_LABELS.get(
            data.get("employmentType"), data.get("employmentType"))
        location_text = " ".join(
            str(x) for x in (summary.get("locationsText"), summary.get("remoteType")) if x)
        remote = (data.get("jobLocationType") == "TELECOMMUTE"
                  or bool(re.search(r"\bremote\b", location_text, re.I)))
        return {
            "job_title": title, "job_remote": int(remote),
            "job_description": description,
            "published_date": data.get("datePosted") or self.get_original_published_date(
                summary.get("postedOn")),
            "employment_type": employment_type, "job_url": job_url,
            "compensation_summary": self.get_salary(description),
        }

    def parse_job_posting(self, html):
        for block in JSON_LD_PATTERN.findall(html):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
        raise ValueError("No JobPosting JSON-LD on the page.")

    def request_job_page(self, job_url, headers, ctx):
        # Page loads look like navigation, not API calls.
        page_headers = {k: v for k, v in headers.items()
                        if k not in ("Origin", "Referer", "Accept")}
        page_headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": None, "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        })
        for _ in range(workday_main.MAX_RETRIES):
            started_at = self.page_limiter.wait()
            with self.session.get(job_url, headers=page_headers, timeout=30) as response:
                if response.status_code != 429:
                    response.raise_for_status()
                    self.page_limiter.on_success()
                    # Workday sends no charset, so requests would guess latin-1.
                    return response.content.decode("utf-8", errors="replace")
                ctx["hit_429"] = True
                self.page_limiter.on_rate_limited(started_at, job_url.split('/')[2])
        raise requests.HTTPError("429 after retries", response=response)

    def data_aquasition(self):
        super().data_aquasition()
        print(f"Job pages parsed: {self.page_hits} | API fallbacks: {self.api_fallbacks}")


def main():
    workday_system = workday_alt_algor()
    workday_system.load_company_slugs()
    workday_system.data_aquasition()


if __name__ == "__main__":
    main()
