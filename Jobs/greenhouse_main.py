import json
import requests
from pathlib import Path
from The_Database.db_connect import database_connection_system
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# color code
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_ORANGE = "\033[38;5;208m"


# global variables
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASHBY_COMPANIES_PATH = PROJECT_ROOT / "Jobs" / "companies" / "greenhouse_comp.json"

class greenhouse_main_system():
    def __init__(self):
        self.database = database_connection_system()

    def load_company_slugs(self):
        with ASHBY_COMPANIES_PATH.open('r',  encoding="utf-8") as companies_file:
            values = json.load(companies_file)

        list_of_slugs = []

        for value in values:
            slug = value.strip()
            list_of_slugs.append(slug)
            self.database.logging_slugs(slug)

        return list_of_slugs 

def main():
    greenhouse_algor = greenhouse_main_system()
    company_slugs = greenhouse_algor.load_company_slugs()

if __name__ == "__main__":
    main()

# python -m Jobs.greenhouse_main