"""Entry point for GitHub Actions: runs every ATS scraper at the same time.

Same flow as Main.py (initialize the database, then scrape), but instead of
running the scrapers one after another, each ATS gets its own worker. iCIMS
keeps its parallel chunk split, and Workday runs Jobs.workday_alt as in Main.py.
"""
from pathlib import Path
import argparse
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from Main import initialize_database, run_icims_parallel  # noqa: E402

SUBPROCESS_SCRAPERS = (
    "Jobs.ashby_main",
    "Jobs.lever_main",
    "Jobs.greenhouse_main",
    "Jobs.bamboohr_main",
    "Jobs.workday_alt",
)

print_lock = threading.Lock()


def log(message, stream=sys.stdout):
    with print_lock:
        print(message, file=stream, flush=True)


def run_module(module):
    log(f"Starting {module}")
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", module],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    # Prefix each line so output from simultaneous scrapers stays readable.
    for line in process.stdout:
        log(f"[{module}] {line.rstrip()}")
    code = process.wait()
    if code:
        raise subprocess.CalledProcessError(code, module)


def run_icims():
    log("Starting Jobs.icims_main (parallel chunks)")
    run_icims_parallel()


def run_all_scrapers():
    jobs = {module: (run_module, module) for module in SUBPROCESS_SCRAPERS}
    jobs["Jobs.icims_main"] = (run_icims,)

    failures = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = {
            name: executor.submit(*job) for name, job in jobs.items()
        }
        # One failing ATS must not stop the others; report all at the end.
        for name, future in futures.items():
            try:
                future.result()
                log(f"Finished {name}")
            except Exception as error:
                failures.append(name)
                log(f"FAILED {name}: {error}", sys.stderr)

    if failures:
        raise RuntimeError(f"Scrapers failed: {', '.join(failures)}")
    log("\nAll scrapers finished successfully.")


def main():
    parser = argparse.ArgumentParser(
        description="Initialize the database and run all job scrapers in parallel."
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Initialize the database without running any scrapers.",
    )
    args = parser.parse_args()

    try:
        initialize_database()
        if not args.init_only:
            run_all_scrapers()
    except KeyboardInterrupt:
        log("\nStopped by user.", sys.stderr)
        return 130
    except Exception as error:
        log(f"\nExecution failed: {error}", sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
