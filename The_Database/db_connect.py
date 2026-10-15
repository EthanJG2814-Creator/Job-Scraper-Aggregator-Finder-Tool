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
        self.db_connection = sqlite3.connect(DB_Path)
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
            was_inserted = True

        except sqlite3.IntegrityError as err:
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
        try: 
            self.cursor.executemany("""
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?,?)
                ON CONFLICT(job_url) DO UPDATE SET
                    last_scanned = excluded.last_scanned
            """, jobs)

            print(f'{ANSI_GREEN} Bulk Insert for Jobs Success! {ANSI_RESET}' )
            self.db_connection.commit()

        except sqlite3.IntegrityError as err:
            self.log_errors.catching_errors(err,"Bulk_insert_job_posting",ATS_Vendor)
            

    def get_company_slug_map(self):
       self.cursor.execute("""
            SELECT company_id, slug_name
            FROM COMPANIES
        """)
       rows = self.cursor.fetchall()
       return {slug_name: company_id for company_id, slug_name in rows}


    def close_database(self):
        self.commit()
        self.cursor.close()
        self.db_connection.close()