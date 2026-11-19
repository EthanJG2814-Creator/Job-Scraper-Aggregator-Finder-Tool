"""Entry point for GitHub Actions: scrape one group of ATS vendors, then parse locations.

Each workflow job runs this file once on its own runner, so each job builds
its own The_Database/job_data.db from scratch.

1. Initialize the database (same as Main.py).
2. Run the chosen scrapers one after another, in the order given. The time
   limit is shared: each scraper gets an equal share of the time that is
   left, so a scraper that finishes early passes its unused time to the ones
   after it. A scraper that hits its share is stopped and the rows it already
   committed are kept (partial data is better than no data).
3. Run Jobs.Locations.locations_func on the database that was just built.

Examples:
    python .github/workflows/github_main.py --scrapers ashby bamboohr greenhouse icims lever
    python .github/workflows/github_main.py --scrapers workday
"""
from pathlib import Path
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from Main import initialize_database  # noqa: E402

DEFAULT_TIME_LIMIT_HOURS = 5.0
STOP_GRACE_SECONDS = 30

# Order here is the default run order.
SCRAPERS = {
    "ashby": ["-m", "Jobs.ashby_main"],
    "bamboohr": ["-m", "Jobs.bamboohr_main"],
    "greenhouse": ["-m", "Jobs.greenhouse_main"],
    # iCIMS splits itself into parallel chunks inside Main.py.
    "icims": ["-c", "from Main import run_icims_parallel; run_icims_parallel()"],
    "lever": ["-m", "Jobs.lever_main"],
    "workday": ["-m", "Jobs.workday_alt"],
}

LOCATIONS_DIR = PROJECT_ROOT / "Jobs" / "Locations"
# GeoNames city lists used by Jobs.Locations.locations_func. Neither is in git.
GAZETTEERS = {
    "cities5000.txt": "https://download.geonames.org/export/dump/cities5000.zip",
    "cities15000.txt": "https://download.geonames.org/export/dump/cities15000.zip",
}

print_lock = threading.Lock()


def log(message, stream=sys.stdout):
    with print_lock:
        print(message, file=stream, flush=True)


def stop_process(process, name):
    """Stop a scraper and everything it spawned; committed rows stay in the database."""
    log(f"Time budget reached; stopping {name} and keeping the data it already saved.")
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_scraper(name, python_args, budget_seconds):
    log(f"\nStarting {name} (time budget {budget_seconds / 3600:.2f} h)")
    process = subprocess.Popen(
        [sys.executable, "-u", *python_args],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    timed_out = threading.Event()

    def on_deadline():
        timed_out.set()
        stop_process(process, name)

    timer = threading.Timer(budget_seconds, on_deadline)
    timer.start()
    try:
        for line in process.stdout:
            log(f"[{name}] {line.rstrip()}")
        code = process.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        return "partial"
    if code:
        raise subprocess.CalledProcessError(code, name)
    return "complete"


def run_scrapers_in_order(names, time_limit_hours):
    deadline = time.monotonic() + time_limit_hours * 3600
    log(f"Running {', '.join(names)} one after another for at most {time_limit_hours:g} hours.")

    results = {}
    for index, name in enumerate(names):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results[name] = "skipped (out of time)"
            log(f"Skipping {name}: no time left.")
            continue
        # Equal share of what is left; unused time rolls over to later scrapers.
        budget = remaining / (len(names) - index)
        # One failing ATS must not stop the others.
        try:
            results[name] = run_scraper(name, SCRAPERS[name], budget)
        except Exception as error:
            results[name] = "failed"
            log(f"FAILED {name}: {error}", sys.stderr)

    log("\nScraper summary:")
    for name, status in results.items():
        log(f"  {name}: {status}")
    if all(status == "failed" for status in results.values()):
        raise RuntimeError("Every scraper failed; there is no data to publish.")


def ensure_gazetteer():
    """The GeoNames city lists are not in git, so download any that are missing on a fresh runner."""
    for txt_name, url in GAZETTEERS.items():
        if (LOCATIONS_DIR / txt_name).is_file():
            continue
        log(f"Downloading {url}")
        archive = LOCATIONS_DIR / Path(url).name
        urllib.request.urlretrieve(url, archive)
        with zipfile.ZipFile(archive) as zipped:
            zipped.extract(txt_name, LOCATIONS_DIR)
        archive.unlink()


def run_location_parser():
    log("\nStarting location parser")
    ensure_gazetteer()
    subprocess.run(
        [sys.executable, "-u", "-m", "Jobs.Locations.locations_func"],
        cwd=PROJECT_ROOT,
        check=True,
    )
    log("Finished location parser")


def main():
    parser = argparse.ArgumentParser(
        description="Initialize the database, run the chosen scrapers in order, then parse locations."
    )
    parser.add_argument(
        "--scrapers",
        nargs="+",
        choices=list(SCRAPERS),
        default=list(SCRAPERS),
        help="Scrapers to run, in this order (default: all of them).",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Initialize the database without running any scrapers.",
    )
    parser.add_argument(
        "--time-limit-hours",
        type=float,
        default=float(os.environ.get("SCRAPE_TIME_LIMIT_HOURS", DEFAULT_TIME_LIMIT_HOURS)),
        help="Total scraping time shared by the chosen scrapers "
        "(default: $SCRAPE_TIME_LIMIT_HOURS or 5).",
    )
    args = parser.parse_args()

    try:
        initialize_database()
        if not args.init_only:
            run_scrapers_in_order(args.scrapers, args.time_limit_hours)
            run_location_parser()
    except KeyboardInterrupt:
        log("\nStopped by user.", sys.stderr)
        return 130
    except Exception as error:
        log(f"\nExecution failed: {error}", sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())