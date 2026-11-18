"""Entry point for GitHub Actions: scrape, parse locations, then publish.

1. Initialize the database (same as Main.py).
2. Run every ATS scraper at the same time, each in its own process, for at
   most SCRAPE_TIME_LIMIT_HOURS. Scrapers still running at the deadline are
   stopped and whatever they already committed is kept (partial data is
   better than no data).
3. Run Jobs.Locations.locations_func so locations are extracted from the
   database. The workflow then publishes the database to GitHub Releases.
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
from concurrent.futures import ThreadPoolExecutor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from Main import initialize_database  # noqa: E402

DEFAULT_TIME_LIMIT_HOURS = 12.0
STOP_GRACE_SECONDS = 30

SCRAPERS = {
    "Jobs.ashby_main": ["-m", "Jobs.ashby_main"],
    "Jobs.lever_main": ["-m", "Jobs.lever_main"],
    "Jobs.greenhouse_main": ["-m", "Jobs.greenhouse_main"],
    "Jobs.bamboohr_main": ["-m", "Jobs.bamboohr_main"],
    "Jobs.workday_alt": ["-m", "Jobs.workday_alt"],
    # iCIMS splits itself into parallel chunks inside Main.py.
    "Jobs.icims_main": ["-c", "from Main import run_icims_parallel; run_icims_parallel()"],
}

LOCATIONS_DIR = PROJECT_ROOT / "Jobs" / "Locations"
GAZETTEER_NAME = "cities5000.txt"
GAZETTEER_URL = "https://download.geonames.org/export/dump/cities5000.zip"

print_lock = threading.Lock()


def log(message, stream=sys.stdout):
    with print_lock:
        print(message, file=stream, flush=True)


def stop_process(process, name):
    """Stop a scraper and everything it spawned; committed rows stay in the database."""
    log(f"Time limit reached; stopping {name} and keeping the data it already saved.")
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_scraper(name, python_args, deadline):
    log(f"Starting {name}")
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

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), on_deadline)
    timer.start()
    try:
        # Prefix each line so output from simultaneous scrapers stays readable.
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


def run_all_scrapers(time_limit_hours):
    deadline = time.monotonic() + time_limit_hours * 3600
    log(f"Scraping for at most {time_limit_hours:g} hours.")

    results = {}
    with ThreadPoolExecutor(max_workers=len(SCRAPERS)) as executor:
        futures = {
            name: executor.submit(run_scraper, name, args, deadline)
            for name, args in SCRAPERS.items()
        }
        # One failing or slow ATS must not stop the others.
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as error:
                results[name] = "failed"
                log(f"FAILED {name}: {error}", sys.stderr)

    log("\nScraper summary:")
    for name, status in results.items():
        log(f"  {name}: {status}")
    if all(status == "failed" for status in results.values()):
        raise RuntimeError("Every scraper failed; there is no data to publish.")


def ensure_gazetteer():
    """The GeoNames city list is not in git, so download it on a fresh runner."""
    if any((LOCATIONS_DIR / name).is_file() for name in (GAZETTEER_NAME, "cities15000.txt")):
        return
    log(f"Downloading {GAZETTEER_URL}")
    archive = LOCATIONS_DIR / "cities5000.zip"
    urllib.request.urlretrieve(GAZETTEER_URL, archive)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extract(GAZETTEER_NAME, LOCATIONS_DIR)
    archive.unlink()


def run_location_parser():
    log("\nStarting location parser for all vendors")
    ensure_gazetteer()
    subprocess.run(
        [sys.executable, "-u", "-m", "Jobs.Locations.locations_func"],
        cwd=PROJECT_ROOT,
        check=True,
    )
    log("Finished location parser for all vendors")


def main():
    parser = argparse.ArgumentParser(
        description="Initialize the database, run all job scrapers in parallel, then parse locations."
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
        help="Stop scrapers after this long and keep partial results "
        "(default: $SCRAPE_TIME_LIMIT_HOURS or 12).",
    )
    args = parser.parse_args()

    try:
        initialize_database()
        if not args.init_only:
            run_all_scrapers(args.time_limit_hours)
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
