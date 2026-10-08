import sqlite3
from datetime import datetime
from pathlib import Path

# global variables to use
PROJECT_ROOT = Path(__file__).resolve().parent
DB_Path = PROJECT_ROOT / "job_data.db"

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

    def create_database_table(self):
        with open("The_Database/sql_code/Table_Create.sql", "r") as file:
            sql_query = file.read()
        self.cursor.execute(sql_query)
        print(f'{ANSI_GREEN} The Database a with its tables have been created {ANSI_RESET}')

    def logging_slugs(self,company_name):
        '''
        This logs the slug name for each company, this can also potentially be the company name as well. 
        '''

        sql = """
        INSERT INTO companies (
            slug_name
        )
        VALUES (?);
        """

        was_inserted = self.execute_write_query(
            sql=sql,
            values=(
                company_name,
            ),
            duplicate_message=(
                f"{company_name} is already in the database. "
                "Skipping."
            ),
        )

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

        print(f'{ANSI_GREEN} Bulk Insert for Ashby Success! {ANSI_RESET}' )
        self.db_connection.commit()

    def bulk_insert_job_possting(self,jobs):
        self.cursor.executemany("""
            INSERT INTO job_postings(
                company_id,   
                job_title,
                job_remote,
                job_description,
                published_date,
                employment_type,
                job_url,
                compensation_summary    

            )
            VALUES(?,?,?,?,?,?,?,?)""", jobs)

        print(f'{ANSI_GREEN} Bulk Insert for Ashby Jobs Success! {ANSI_RESET}')
        self.db_connection.commit()

    def get_company_slug_map(self):
       self.cursor.execute("""
            SELECT company_id, slug_name
            FROM COMPANIES
        """)
       rows = self.cursor.fetchall()
       return {slug_name: company_id for company_id, slug_name in rows}
        
    def execute_write_query(
            self,
            sql: str,
            values: tuple,
            duplicate_message: str | None = None,
        ) -> bool:
            """
            Reusable method for INSERT, UPDATE, and DELETE operations.

            Uses a SAVEPOINT instead of commit()/rollback() so a duplicate row can be
            undone without fsync-ing to disk on every single call. Call commit()
            periodically (or once at the end) to actually persist the writes.
            """

            try:
                self.cursor.execute("SAVEPOINT write_query;")
                self.cursor.execute(sql, values)
                self.cursor.execute("RELEASE write_query;")

                return True

            except sqlite3.IntegrityError:
                self.cursor.execute("ROLLBACK TO write_query;")
                self.cursor.execute("RELEASE write_query;")

                if duplicate_message:
                    print(
                        f"{ANSI_YELLOW}"
                        f"{duplicate_message}"
                        f"{ANSI_RESET}"
                    )
                else:
                    print(
                        f"{ANSI_YELLOW}"
                        "Database constraint error. Skipping."
                        f"{ANSI_RESET}"
                    )

                return False

            except sqlite3.Error as error:
                self.cursor.execute("ROLLBACK TO write_query;")
                self.cursor.execute("RELEASE write_query;")

                print(
                    f"{ANSI_RED}"
                    f"Database error: {error}"
                    f"{ANSI_RESET}"
                )

                return False   
        
    def close_database(self):
        self.commit()
        self.cursor.close()
        self.db_connection.close()