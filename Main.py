from pathlib import Path
import argparse
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from Jobs.icims_main import icims_main_system


PROJECT_ROOT = Path(__file__).resolve().parent
DATABASE_PATH = PROJECT_ROOT / "The_Database" / "job_data.db"
SCHEMA_PATH = (
    PROJECT_ROOT / "The_Database" / "sql_code" / "Table_Create.sql"
)

SEQUENTIAL_SCRAPERS = (
    "Jobs.ashby_main",
    "Jobs.lever_main",
    "Jobs.greenhouse_main",
    "Jobs.bamboohr_main",
)

ICIMS_CHUNKS = 4


def initialize_database():
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.executescript(schema_sql)
    finally:
        connection.close()

    print(f"Database initialized: {DATABASE_PATH}", flush=True)


def run_icims_chunk(slug_chunk):
    # Own instance per thread because SQLite connections are single-thread.
    icims_main_system().data_aquasition(slug_chunk)


def run_icims_parallel():
    slugs = icims_main_system().load_company_slugs()
    # Round-robin so every chunk gets a similar mix of big and small companies.
    chunks = [slugs[i::ICIMS_CHUNKS] for i in range(ICIMS_CHUNKS)]
    chunks = [chunk for chunk in chunks if chunk]

    with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
        futures = [executor.submit(run_icims_chunk, chunk) for chunk in chunks]
        for future in futures:
            future.result()


def run_scrapers():
    total = len(SEQUENTIAL_SCRAPERS) + 2

    for number, scraper in enumerate(SEQUENTIAL_SCRAPERS, start=1):
        print(
            f"\n[{number}/{total}] Starting {scraper}",
            flush=True,
        )

        subprocess.run(
            [sys.executable, "-u", "-m", scraper],
            cwd=PROJECT_ROOT,
            check=True,
        )

        print(f"Finished {scraper}", flush=True)

    icims_step = len(SEQUENTIAL_SCRAPERS) + 1
    print(
        f"\n[{icims_step}/{total}] Starting Jobs.icims_main "
        f"({ICIMS_CHUNKS} parallel chunks)",
        flush=True,
    )
    run_icims_parallel()
    print("Finished Jobs.icims_main", flush=True)

    print(f"\n[{total}/{total}] Starting Jobs.workday_alt", flush=True)
    subprocess.run(
        [sys.executable, "-u", "-m", "Jobs.workday_alt"],
        cwd=PROJECT_ROOT,
        check=True,
    )
    print("Finished Jobs.workday_alt", flush=True)

    print("\nAll scrapers finished successfully.", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Initialize the database and run job scrapers."
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
            run_scrapers()

    except subprocess.CalledProcessError as error:
        print(
            f"\nScraper failed with exit code {error.returncode}. "
            "Remaining scrapers were not started.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr, flush=True)
        return 130

    except Exception as error:
        print(f"\nExecution failed: {error}", file=sys.stderr, flush=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())