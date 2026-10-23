import itertools
import json
import re
import random
import time
import requests
from pathlib import Path
from queue import Queue, Empty
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta
from lxml import html as lxml_html
from requests.adapters import HTTPAdapter
from The_Database.db_connect import database_connection_system
from logging_errors import error_code

PROJECT_ROOT = Path(__file__).resolve().parents[1]
workday_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "workday.json"
ERRORS = (requests.RequestException, ValueError, KeyError, TypeError,
          AttributeError, IndexError)

MAX_WORKERS = 75
# Workday's CXS API rejects any limit above 20 with HTTP 400.
PAGE_LIMIT = 20
MAX_RETRIES = 5

START_INTERVAL = 0.5
MIN_INTERVAL = 0.01
MAX_INTERVAL = 20.0
# A slug that draws this many 429s is abandoned (kept as partial/429, retried next run).
MAX_SLUG_429S = 15
CLEAN_SLUGS_TO_SPEED_UP = 10
SPEED_UP_FACTOR = 0.9
SLOW_DOWN_FACTOR = 2.0
COOLDOWN_SECONDS = 10

# Fraction of random +/- variation applied to each request spacing.
INTERVAL_JITTER = 0.5

COMMON_HEADERS = {
    "Content-Type": "application/json",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

try:
    import brotli  # noqa: F401
    ACCEPT_ENCODINGS = ["gzip, deflate, br", "gzip, deflate"]
except ImportError:
    # requests can't decode br/zstd without extra packages, so don't advertise them.
    ACCEPT_ENCODINGS = ["gzip, deflate"]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9", "en-US,en;q=0.9,es;q=0.8", "en-US", "en-GB,en;q=0.9",
    "en-US,en;q=0.8", "en-CA,en;q=0.9", "en-US,en;q=0.9,fr;q=0.7",
    "en-AU,en;q=0.9", "en,en-US;q=0.9",
]
ACCEPT_VALUES = ["application/json, text/plain, */*", "application/json", "*/*"]

CHROMIUM_VERSIONS = list(range(118, 130))
FIREFOX_VERSIONS = list(range(115, 130))
SAFARI_VERSIONS = ["16.6", "17.0", "17.1", "17.2", "17.3", "17.4", "17.5", "17.6"]
DESKTOP_OSES = [
    ("Windows NT 10.0; Win64; x64", "Windows"),
    ("Macintosh; Intel Mac OS X 10_15_7", "macOS"),
    ("X11; Linux x86_64", "Linux"),
    ("X11; Ubuntu; Linux x86_64", "Linux"),
]
CHROMIUM_BRANDS = ["Google Chrome", "Microsoft Edge", "Chromium"]


def random_browser_headers():
    """Build one internally consistent browser identity. User-Agent and the
    sec-ch-ua client hints must agree, and Firefox/Safari send no client hints."""
    family = random.choices(
        ["chromium", "firefox", "safari"], weights=[70, 20, 10])[0]
    headers = {
        "Accept": random.choice(ACCEPT_VALUES),
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": random.choice(ACCEPT_ENCODINGS),
    }
    os_token, platform = random.choice(DESKTOP_OSES)
    if family == "chromium":
        version = random.choice(CHROMIUM_VERSIONS)
        brand = random.choice(CHROMIUM_BRANDS)
        edge = brand == "Microsoft Edge"
        headers["User-Agent"] = (
            f"Mozilla/5.0 ({os_token}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version}.0.0.0 Safari/537.36" + (f" Edg/{version}.0.0.0" if edge else ""))
        grease = random.choice(['"Not/A)Brand";v="8"', '"Not_A Brand";v="24"',
                                '"Not;A=Brand";v="99"'])
        hints = [grease, f'"Chromium";v="{version}"']
        if brand != "Chromium":
            hints.append(f'"{brand}";v="{version}"')
        random.shuffle(hints)
        headers["sec-ch-ua"] = ", ".join(hints)
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = f'"{platform}"'
    elif family == "firefox":
        version = random.choice(FIREFOX_VERSIONS)
        headers["User-Agent"] = (
            f"Mozilla/5.0 ({os_token.replace('Intel Mac OS X 10_15_7', 'Intel Mac OS X 10.15')}"
            f"; rv:{version}.0) Gecko/20100101 Firefox/{version}.0")
    else:
        version = random.choice(SAFARI_VERSIONS)
        headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            f"(KHTML, like Gecko) Version/{version} Safari/605.1.15")
    return headers


# color code values
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_ORANGE = "\033[38;5;208m"


_CURRENCY = r"(?:USD|CAD|AUD|NZD|EUR|GBP|CHF|INR|JPY|SGD|HKD|[$£€])"
_AMOUNT = r"\d[\d,]*(?:\.\d+)?(?:[kK]\b)?"
SALARY_PATTERN = re.compile(
    rf"{_CURRENCY}\s*{_AMOUNT}(?:\s*{_CURRENCY})?\s*"
    rf"(?:[-–—]|\bto\b)\s*(?:{_CURRENCY}\s*)?{_AMOUNT}"
    rf"(?:\s*{_CURRENCY})?(?:\s*(?:per|/)\s*(?:hour|year|month|annum))?",
    re.I,
)


def html_to_text(markup):
    """Plain text of an HTML fragment, with pieces joined by single spaces."""
    if not markup or not markup.strip():
        return ""
    root = lxml_html.fragment_fromstring(markup, create_parent="div")
    parts = root.xpath("//text()[not(ancestor::script or ancestor::style)]")
    return " ".join(part.strip() for part in parts if part.strip())


class RateLimiter:
    """One limiter shared by every worker: spaces requests, speeds up after
    clean slugs, and slows down plus a fixed cooldown after a 429. Each 429
    also records how many requests succeeded since the last one, so the
    server's limit can be estimated as a running average."""

    def __init__(self, name="listing API"):
        self.name = name
        self.lock = Lock()
        self.interval = START_INTERVAL
        self.next_request_at = 0.0
        self.cooldown_until = 0.0
        self.clean_slugs = 0
        self.started_at = time.monotonic()
        self.total_requests = 0
        self.window_start = self.started_at
        self.window_successes = 0
        # Running totals instead of a list that would grow for the whole run.
        self.sample_successes = self.sample_seconds = self.sample_count = 0

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                ready_at = max(self.next_request_at, self.cooldown_until)
                if now >= ready_at:
                    jitter = random.uniform(1 - INTERVAL_JITTER, 1 + INTERVAL_JITTER)
                    self.next_request_at = now + self.interval * jitter
                    return now
                sleep_for = ready_at - now
            time.sleep(sleep_for)

    def on_success(self):
        with self.lock:
            self.window_successes += 1
            self.total_requests += 1

    def guess_text(self):
        """Average of every (successes, seconds) window that ended in a 429."""
        if self.sample_count == 0 or self.sample_seconds <= 0:
            return "limit not measured yet"
        rate = self.sample_successes / self.sample_seconds
        return (f"guessed limit: {rate * 30:.0f} requests per 30s "
                f"({rate:.2f}/s, {self.sample_count} samples)")

    def on_rate_limited(self, started_at, where=""):
        with self.lock:
            # Requests already in flight when a cooldown began must not retrigger it.
            if started_at < self.cooldown_until:
                return
            now = time.monotonic()
            elapsed = now - self.window_start
            if self.window_successes > 0 and elapsed > 1:
                self.sample_successes += self.window_successes
                self.sample_seconds += elapsed
                self.sample_count += 1
            self.interval = min(MAX_INTERVAL, self.interval * SLOW_DOWN_FACTOR)
            self.clean_slugs = 0
            self.cooldown_until = now + COOLDOWN_SECONDS
            self.window_start = self.cooldown_until
            self.window_successes = 0
            print(f"{ANSI_ORANGE} 429 on {self.name} ({where}) | slowing down to {self.interval:.3f}s per request, "
                  f"cooling down {COOLDOWN_SECONDS}s | {self.guess_text()} {ANSI_RESET}")

    def on_clean_slug(self):
        with self.lock:
            self.clean_slugs += 1
            if self.clean_slugs >= CLEAN_SLUGS_TO_SPEED_UP:
                self.interval = max(MIN_INTERVAL, self.interval * SPEED_UP_FACTOR)
                self.clean_slugs = 0
                print(f"{ANSI_ORANGE} Speeding up | {self.interval:.3f}s per request | "
                      f"{self.guess_text()} {ANSI_RESET}")


class SlugGaveUp(Exception):
    """Raised when one slug has drawn too many 429s; deliberately not in ERRORS
    so it aborts the whole slug instead of just one job."""


class LimiterPool:
    """One RateLimiter per host. Workday limits per tenant, so a 429 from one
    tenant slows only that tenant and never the rest of the pool."""

    def __init__(self, name, start_interval=START_INTERVAL):
        self.name = name
        self.start_interval = start_interval
        self.lock = Lock()
        self.limiters = {}
        self.started_at = time.monotonic()

    def get(self, host):
        with self.lock:
            if host not in self.limiters:
                limiter = RateLimiter(f"{self.name} {host}")
                limiter.interval = self.start_interval
                self.limiters[host] = limiter
            return self.limiters[host]

    def overall_rate(self):
        with self.lock:
            total = sum(l.total_requests for l in self.limiters.values())
        elapsed = time.monotonic() - self.started_at
        return total / elapsed if elapsed > 0 else 0.0


def interleave_by_host(slugs):
    """Round-robin slugs across hosts so workers rarely queue on one tenant."""
    groups = {}
    for slug in slugs:
        host = slug.split("|")[0].strip().lower()
        groups.setdefault(host, []).append(slug)
    ordered = []
    queues = list(groups.values())
    while queues:
        ordered.extend(q.pop(0) for q in queues)
        queues = [q for q in queues if q]
    return ordered


class workday_algor:
    def __init__(self):
        self.database = database_connection_system()
        self.list_of_slugs = []
        self.jobs_worked = self.jobs_failed = 0
        # Workers can't touch SQLite, so they hand finished pages to the main thread.
        self.save_queue = Queue()
        self.failed_saves = set()
        self.limiters = LimiterPool("listing API")
        self.session = requests.Session()
        self.session.headers.update(COMMON_HEADERS)
        adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
        self.session.mount("https://", adapter)

    def load_company_slugs(self):
        with workday_COMPANIES_PATH.open(encoding="utf-8") as file:
            self.list_of_slugs = [value.strip() for value in json.load(file)]
        for slug in self.list_of_slugs:
            self.database.logging_slugs(slug, "Workday")

    def data_aquasition(self):
        company_map = self.database.get_company_slug_map()
        done_today = self.database.get_slugs_scraped_today("Workday")
        dead = self.database.get_dead_slugs("Workday")
        before = len(self.list_of_slugs)
        self.list_of_slugs = [s for s in self.list_of_slugs
                              if s not in done_today and s not in dead]
        print(f"{ANSI_YELLOW}Skipping {before - len(self.list_of_slugs)} slugs "
              f"({len(dead)} dead, rest already finished today){ANSI_RESET}")
        self.list_of_slugs = interleave_by_host(self.list_of_slugs)
        total = len(self.list_of_slugs)
        # Loaded here because SQLite connections can't be used from worker threads.
        self.known_urls = self.database.get_known_job_urls()
        processed = 0

        # Keep only a bounded window of slugs in flight. Holding every future (as
        # as_completed does) would keep every downloaded description in memory.
        slugs = iter(self.list_of_slugs)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            pending = {}
            for slug in itertools.islice(slugs, MAX_WORKERS * 2):
                pending[executor.submit(self.slug_processing, slug)] = slug
            # SQLite connections are single-thread, so only this thread writes.
            while pending:
                done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                self.drain_save_queue(company_map)
                for future in done:
                    slug = pending.pop(future)
                    next_slug = next(slugs, None)
                    if next_slug is not None:
                        pending[executor.submit(self.slug_processing, next_slug)] = next_slug
                    processed += 1
                    try:
                        result = future.result()
                        saved = self.save_result(company_map, result)
                    except Exception as error:
                        # One bad slug must never kill an overnight run.
                        self.database.log_errors.unexpected_slug_error(
                            error, "data_aquasition", "Workday",
                            slug, f"[{processed}/{total}]",
                        )
                        continue
                    finally:
                        del future
                    ok = result["status"] == "success"
                    self.jobs_worked += ok
                    self.jobs_failed += not ok
                    if ok and saved and not result["hit_429"]:
                        self.limiters.get(self.host_of(slug)).on_clean_slug()

                    color = ANSI_GREEN if ok else ANSI_YELLOW
                    print(f"{color}[{processed}/{total}] {slug} | "
                          f"{result['status']} | {result['job_count']} jobs | "
                          f"{self.limiters.overall_rate():.2f} req/s overall{ANSI_RESET}")
                    del result

    @staticmethod
    def host_of(slug):
        company, wd, _ = [part.strip() for part in slug.split("|")]
        return f"{company}.{wd}.myworkdayjobs.com"

    def drain_save_queue(self, company_map):
        while True:
            try:
                slug, jobs, known_urls = self.save_queue.get_nowait()
            except Empty:
                return
            try:
                if not self.save_jobs(company_map[slug], jobs, known_urls):
                    self.failed_saves.add(slug)
            except Exception as error:
                self.failed_saves.add(slug)
                self.database.log_errors.unexpected_slug_error(
                    error, "drain_save_queue", "Workday", slug, "")

    def save_jobs(self, company_id, jobs, known_urls):
        saved = True
        scanned = datetime.now().astimezone().isoformat(timespec="seconds")
        if jobs:
            job_rows = [(
                company_id, job["job_title"], job["job_remote"],
                job["job_description"], job["published_date"],
                job["employment_type"], job["job_url"],
                job["compensation_summary"], scanned,
            ) for job in jobs]
            saved = self.database.bulk_insert_job_possting(job_rows, "Workday")
            self.known_urls.update(job["job_url"] for job in jobs)
        if known_urls:
            self.database.bulk_touch_job_postings(known_urls, scanned)
        return saved

    def save_result(self, company_map, result):
        """Logs the finished slug. Its jobs were already saved page by page."""
        company_id = company_map[result["company_slug"]]
        self.database.bulk_insert_scrape_logs([(
            company_id, result["ats_vendor"], result["status"],
            result["job_count"], result["error"],
        )])
        return result["company_slug"] not in self.failed_saves

    def slug_processing(self, slug):
        jobs, seen_paths, known_urls = [], set(), []
        jobs_found = 0
        total, error_type = None, None
        ctx = {"hit_429": False, "count_429": 0}
        try:
            company, wd, site_id = [part.strip() for part in slug.split("|")]
            base_url = f"https://{company}.{wd}.myworkdayjobs.com"
            site_api = f"{base_url}/wday/cxs/{company}/{site_id}"
            # One browser identity per slug, so its requests look like one visitor.
            headers = {**random_browser_headers(),
                       "Origin": base_url, "Referer": f"{base_url}/{site_id}"}
            offset = 0
            while True:
                data = self.request_workday_page(f"{site_api}/jobs", headers, ctx, {
                    "appliedFacets": {}, "limit": PAGE_LIMIT,
                    "offset": offset, "searchText": "",
                })
                page_jobs = data["jobPostings"]
                if total is None:
                    total = data["total"]
                if not page_jobs:
                    if len(seen_paths) < total:
                        raise ValueError("Pagination ended early.")
                    break
                new_jobs = 0
                for job in page_jobs:
                    path = job["externalPath"]
                    if path in seen_paths:
                        continue
                    seen_paths.add(path)
                    new_jobs += 1
                    job_url = f"{base_url}/{site_id}{path}"
                    # Already stored: the listing proves it is still live.
                    if job_url in self.known_urls:
                        known_urls.append(job_url)
                        continue
                    try:
                        jobs.append(self.get_job_detail(
                            f"{site_api}{path}", job_url, job, headers, ctx,
                        ))
                        jobs_found += 1
                    except ERRORS as error:
                        error_type = error_type or error_code(error)
                # Hand the page to the main thread to save, then forget it.
                if jobs or known_urls:
                    self.save_queue.put((slug, jobs, known_urls))
                    jobs, known_urls = [], []
                if not new_jobs:
                    raise ValueError("Pagination returned a repeated page.")
                if len(seen_paths) >= total:
                    break
                offset += len(page_jobs)
        except SlugGaveUp:
            error_type = "429"
        except ERRORS as error:
            error_type = error_type or error_code(error)
        # Flush what the interrupted page had already collected.
        if jobs or known_urls:
            self.save_queue.put((slug, jobs, known_urls))
        return {
            "company_slug": slug, "ats_vendor": "Workday",
            "status": ("success" if error_type is None
                       else "partial" if jobs_found else "fail"),
            "job_count": len(seen_paths), "error": error_type,
            "hit_429": ctx["hit_429"],
        }

    def get_job_detail(self, detail_url, job_url, summary, headers, ctx):
        info = self.request_workday_page(detail_url, headers, ctx)["jobPostingInfo"]
        title = info.get("title") or summary.get("title")
        description = html_to_text(info.get("jobDescription"))
        if not title or not description:
            raise ValueError("Job detail is missing title or description.")
        employment_type = info.get("timeType") or info.get("workerSubType")
        remote_label = str(info.get("remoteType") or summary.get("remoteType")
                           or employment_type or "")
        remote = bool(re.search(r"\bremote\b", remote_label, re.I))
        if re.search(r"\b(?:non[- ]?remote|not remote)\b", remote_label, re.I):
            remote = False

        return {
            "job_title": title, "job_remote": int(remote),
            "job_description": description,
            "published_date": self.get_original_published_date(
                info.get("postedOn") or summary.get("postedOn")
            ),
            "employment_type": employment_type, "job_url": job_url,
            "compensation_summary": self.get_salary(description),
        }

    def get_salary(self, description):
        match = SALARY_PATTERN.search(description)
        return match.group(0) if match else None

    def get_original_published_date(self, posted_on):
        text = str(posted_on or "").strip().casefold()
        today = datetime.now().date()
        if text in ("posted today", "today"):
            return today.isoformat()
        if text in ("posted yesterday", "yesterday"):
            return (today - timedelta(days=1)).isoformat()
        match = re.fullmatch(r"(?:posted\s+)?(\d+)\s+days?\s+ago", text)
        if match and int(match.group(1)) < 30:
            return (today - timedelta(days=int(match.group(1)))).isoformat()
        return None

    def request_with_retries(self, limiters, method, url, ctx, **kwargs):
        """Sends one request through the per-host limiter, retrying on 429 and
        giving up on the whole slug after MAX_SLUG_429S. Returns the response."""
        host = url.split('/')[2]
        limiter = limiters.get(host)
        for _ in range(MAX_RETRIES):
            started_at = limiter.wait()
            with self.session.request(method, url, timeout=30, **kwargs) as response:
                if response.status_code != 429:
                    response.raise_for_status()
                    limiter.on_success()
                    return response
                ctx["hit_429"] = True
                ctx["count_429"] += 1
                limiter.on_rate_limited(started_at, host)
                if ctx["count_429"] >= MAX_SLUG_429S:
                    raise SlugGaveUp(url)
        raise requests.HTTPError("429 after retries", response=response)

    def request_workday_page(self, api_url, headers, ctx, payload=None):
        method = "GET" if payload is None else "POST"
        return self.request_with_retries(
            self.limiters, method, api_url, ctx, json=payload, headers=headers,
        ).json()


def main():
    workday_system = workday_algor()
    workday_system.load_company_slugs()
    workday_system.data_aquasition()


if __name__ == "__main__":
    main()
