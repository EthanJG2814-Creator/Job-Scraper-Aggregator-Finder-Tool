import json
import random
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASHBY_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "ashby.json"
OUTPUT_PATH = PROJECT_ROOT / "Jobs" / "ashby_output.txt"


def load_company_slugs():
    with open(ASHBY_COMPANIES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("ashby.json must contain a list of slugs")

    slugs = [x.strip() for x in data if isinstance(x, str) and x.strip()]
    if not slugs:
        raise ValueError("No valid slugs found in ashby.json")

    return slugs


def main():
    slugs = load_company_slugs()
    slug = random.choice(slugs)

    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

    response = requests.get(
        url,
        headers={"User-Agent": "JobScraperV4/0.1", "Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    jobs = payload.get("jobs", [])

    lines = []
    lines.append(f"Random Ashby company slug: {slug}")
    lines.append(f"Request URL: {url}")
    lines.append(f"HTTP status: {response.status_code}")
    lines.append("")

    lines.append("TOP-LEVEL JSON KEYS:")
    for key in payload.keys():
        lines.append(f" - {key}")

    lines.append("")
    lines.append(f"Total jobs returned: {len(jobs)}")

    if not jobs:
        lines.append("")
        lines.append("No jobs returned for this slug.")
    else:
        sample_job = jobs[0]

        lines.append("")
        lines.append("SAMPLE JOB KEYS:")
        for key in sample_job.keys():
            lines.append(f" - {key}")

        lines.append("")
        lines.append("FULL SAMPLE JOB JSON:")
        lines.append("")
        lines.append(json.dumps(sample_job, indent=2, ensure_ascii=False))

    OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved output to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()


# python -m Jobs.ashby_test