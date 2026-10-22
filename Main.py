from pathlib import Path
import argparse
import sqlite3
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
DATABASE_PATH = PROJECT_ROOT / "The_Database" / "job_data.db"
SCHEMA_PATH = (
    PROJECT_ROOT / "The_Database" / "sql_code" / "Table_Create.sql"
)

SCRAPERS = (
    "Jobs.ashby_main",
    "Jobs.bamboohr_main",
    "Jobs.greenhouse_main",
    "Jobs.icims_main",
    "Jobs.lever_main",
    "Jobs.workday_alt",
)


def initialize_database():
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.executescript(schema_sql)
    finally:
        connection.close()

    print(f"Database initialized: {DATABASE_PATH}", flush=True)


def run_scrapers():
    for number, scraper in enumerate(SCRAPERS, start=1):
        print(
            f"\n[{number}/{len(SCRAPERS)}] Starting {scraper}",
            flush=True,
        )

        subprocess.run(
            [sys.executable, "-u", "-m", scraper],
            cwd=PROJECT_ROOT,
            check=True,
        )

        print(f"Finished {scraper}", flush=True)

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