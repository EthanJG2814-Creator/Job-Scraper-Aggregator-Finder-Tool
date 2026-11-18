from pathlib import Path
import argparse
import os
import sqlite3
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

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

MATRIX_SCRIPT = PROJECT_ROOT / "Job Matrix" / "job_matrix.py"


def initialize_database():
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.executescript(schema_sql)
    finally:
        connection.close()

    print(f"Database initialized: {DATABASE_PATH}", flush=True)


def run_icims_chunk(slug_chunk, write_lock):
    # Own instance per thread because SQLite connections are single-thread.
    with write_lock:
        scraper = icims_main_system(write_lock=write_lock)
    try:
        scraper.data_aquasition(slug_chunk)
    except BaseException:
        scraper.database.db_connection.rollback()
        raise
    finally:
        scraper.database.close_database()


def run_icims_parallel():
    scraper = icims_main_system()
    try:
        slugs = scraper.load_company_slugs()
    finally:
        scraper.database.close_database()
    # Round-robin so every chunk gets a similar mix of big and small companies.
    chunks = [slugs[i::ICIMS_CHUNKS] for i in range(ICIMS_CHUNKS)]
    chunks = [chunk for chunk in chunks if chunk]

    if not chunks:
        return
    write_lock = Lock()
    with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
        futures = [executor.submit(run_icims_chunk, chunk, write_lock) for chunk in chunks]
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

    print("\nStarting location parser for all vendors", flush=True)
    subprocess.run(
        [sys.executable, "-u", "-m", "Jobs.Locations.locations_func"],
        cwd=PROJECT_ROOT,
        check=True,
    )
    print("Finished location parser for all vendors", flush=True)


def matrix_python():
    """Interpreter with the Job Matrix libraries: JOB_MATRIX_PYTHON, else the embedding-models venv."""
    candidates = []
    if os.environ.get("JOB_MATRIX_PYTHON"):
        candidates.append(Path(os.environ["JOB_MATRIX_PYTHON"]))
    venv = PROJECT_ROOT.parent / "embedding models" / ".venv"
    candidates += [venv / "bin" / "python", venv / "Scripts" / "python.exe"]
    return next((str(path) for path in candidates if path.is_file()), None)


def update_job_matrix():
    """Refresh the Job Matrix from the new postings. A failure here never fails the scrape."""
    python = matrix_python()
    if python is None:
        print(
            "\nSkipping the Job Matrix update: set JOB_MATRIX_PYTHON to a Python with "
            "sentence-transformers installed.",
            file=sys.stderr,
            flush=True,
        )
        return
    print("\nUpdating the Job Matrix", flush=True)
    result = subprocess.run(
        [python, "-u", str(MATRIX_SCRIPT), "update"], cwd=PROJECT_ROOT
    )
    if result.returncode:
        print(
            f"Job Matrix update failed with exit code {result.returncode}; "
            "the scrape itself completed.",
            file=sys.stderr,
            flush=True,
        )
    else:
        print("Finished Job Matrix update", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Initialize the database and run job scrapers."
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Initialize the database without running any scrapers.",
    )
    parser.add_argument(
        "--skip-matrix",
        action="store_true",
        help="Do not refresh the Job Matrix after the scrapers finish.",
    )
    args = parser.parse_args()

    try:
        initialize_database()

        if not args.init_only:
            run_scrapers()
            if not args.skip_matrix:
                update_job_matrix()

    except subprocess.CalledProcessError as error:
        print(
            f"\nScraper or location parser failed with exit code {error.returncode}. "
            "The run did not complete successfully.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr, flush=True)
        return 130

    except Exception as error:
        print(f"\nExecution failed: {error}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

    # to execute the main file:
    # python -m Main

    # to execute the main file, without the matrix
    # python Main.py --skip-matrix 