import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import json
from logging_errors import Error_Log_system

# global variables to use
PROJECT_ROOT = Path(__file__).resolve().parent
DB_Path = PROJECT_ROOT / "job_data.db"
ERROR_LOG_PATH = PROJECT_ROOT / "logs" / "database_errors.txt"


# color code
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_ORANGE = "\033[38;5;208m"


class database_connection_system:
    def __init__(self):
        self.db_connection = sqlite3.connect(DB_Path, timeout=120)
        self.db_connection.execute("PRAGMA journal_mode=WAL")
        self.db_connection.execute("PRAGMA busy_timeout=120000")
        self.cursor = self.db_connection.cursor()
        self.log_errors = Error_Log_system()

    def create_database_table(self):
        with open("The_Database/sql_code/Table_Create.sql", "r") as file:
            sql_query = file.read()
        self.cursor.execute(sql_query)
        print(f'{ANSI_GREEN} The Database a with its tables have been created {ANSI_RESET}')

    def logging_slugs(self,company_name,ATS_VENDOR):
        '''
        This logs the slug name for each company, this can also potentially be the company name as well. 
        '''

        sql = """
        INSERT INTO companies (
            slug_name
        )
        VALUES (?);
        """

        try:
            self.cursor.execute(sql,(company_name,))
            self.db_connection.commit()
            was_inserted = True

        except sqlite3.IntegrityError as err:
            # Without this the failed INSERT keeps a write lock open.
            self.db_connection.rollback()
            self.log_errors.catching_errors(err,"logging_slugs",ATS_VENDOR,company_name)
            was_inserted = False

        if was_inserted:
            print(
                f"{ANSI_GREEN}"
                f"ADDED | {company_name}"
                f"{ANSI_RESET}"
            )

    def bulk_insert_scrape_logs(self, rows):
        self.cursor.executemany("""
            INSERT INTO sources (
                company_id,
                ats_vendor,
                status,
                job_count,
                error
            )
            VALUES (?, ?, ?, ?, ?)
        """, rows)

        print(f'{ANSI_GREEN} Bulk Insert for logs Success! {ANSI_RESET}' )
        self.db_connection.commit()

    def bulk_insert_job_possting(self, jobs, ATS_Vendor):
        sql = """
            INSERT INTO job_postings (
                company_id,
                job_title,
                job_remote,
                job_description,
                published_date,
                employment_type,
                job_url,
                compensation_summary,
                last_scanned
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_url) DO UPDATE SET
                last_scanned = excluded.last_scanned
        """
        try:
            self.cursor.executemany(sql, jobs)
            self.db_connection.commit()
            print(f'{ANSI_GREEN} Bulk Insert for Jobs Success! {ANSI_RESET} ({len(jobs)} rows)')
            return True
        except sqlite3.Error as err:
            self.db_connection.rollback()
            self.log_errors.catching_errors(err, "Bulk_insert_job_posting", ATS_Vendor)

        # Fall back to row-by-row so one bad row doesn't drop the whole batch
        inserted = 0
        for job in jobs:
            try:
                self.cursor.execute(sql, job)
                inserted += 1
            except sqlite3.Error as err:
                self.log_errors.catching_errors(err, "Bulk_insert_job_posting_row", ATS_Vendor, job[6])
        self.db_connection.commit()
        print(f'{ANSI_YELLOW} Fallback insert for Jobs: {inserted}/{len(jobs)} rows {ANSI_RESET}')
        return inserted == len(jobs)

    def get_dead_slugs(self, ats_vendor):
        """Slugs whose most recent scrape failed with 404/410 before any job was
        listed, so the tenant or site no longer exists. They are skipped entirely;
        a later non-dead result for the slug un-kills it."""
        self.cursor.execute("""
            SELECT c.slug_name
            FROM sources s
            JOIN companies c ON c.company_id = s.company_id
            WHERE s.ats_vendor = ?
              AND s.source_id = (SELECT MAX(s2.source_id) FROM sources s2
                                 WHERE s2.company_id = s.company_id
                                   AND s2.ats_vendor = s.ats_vendor)
              AND s.status = 'fail'
              AND s.error IN ('404', '410')
              AND COALESCE(s.job_count, 0) = 0
        """, (ats_vendor,))
        return {row[0] for row in self.cursor.fetchall()}

    def get_slugs_scraped_today(self, ats_vendor):
        """Slugs finished today that should not be re-run: successes, plus
        failures whose error wasn't a 429. Partials and 429 failures are retried."""
        self.cursor.execute("""
            SELECT DISTINCT c.slug_name
            FROM sources s
            JOIN companies c ON c.company_id = s.company_id
            WHERE s.ats_vendor = ?
              AND (s.status = 'success'
                   OR (s.status = 'fail' AND COALESCE(s.error, '') <> '429'))
              AND date(s.created_at, 'localtime') = date('now', 'localtime')
        """, (ats_vendor,))
        return {row[0] for row in self.cursor.fetchall()}

    def get_known_job_urls(self):
        self.cursor.execute("SELECT job_url FROM job_postings")
        return {row[0] for row in self.cursor.fetchall()}

    def bulk_touch_job_postings(self, urls, scanned):
        self.cursor.executemany(
            "UPDATE job_postings SET last_scanned = ? WHERE job_url = ?",
            [(scanned, url) for url in urls],
        )
        self.db_connection.commit()

    def get_company_slug_map(self):
       self.cursor.execute("""
            SELECT company_id, slug_name
            FROM COMPANIES
        """)
       rows = self.cursor.fetchall()
       return {slug_name: company_id for company_id, slug_name in rows}


    def close_database(self):
        self.db_connection.commit()
        self.cursor.close()
        self.db_connection.close()