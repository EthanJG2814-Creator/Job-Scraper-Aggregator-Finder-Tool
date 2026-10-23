# Introduction 

Job searching can be time-consuming, especially when relevant openings are
scattered across many company career pages and job boards. This project brings
job postings from several hiring platforms together in one searchable database,
so you can explore opportunities without checking every site individually.

The scraper collects postings from supported applicant-tracking systems,
including Ashby, BambooHR, Greenhouse, iCIMS, Lever, and Workday. It stores
details such as job title, company, description, remote status, employment
type, compensation when available, posting date, and job URL. The database
helps avoid adding the same posting more than once and records when a posting
was last seen by the scraper.

The included website lets you search postings by job title and by words in the
description, review job details, and sort results by publication or scan date.
The collected data can also support further analysis, such as exploring
compensation or identifying postings that have remained active for an unusually
long time. These are potential uses of the data, not currently automated
features of the website.

This project is still in development. The latest GitHub release may include a
database containing more than 1.1 million previously collected postings, so
you can start exploring existing results without waiting for a full scrape.

# How the scraper works

## Company configuration

The files in `Jobs/companies/` contain the company slugs used to locate each
employer's job board on a supported applicant-tracking system (ATS). A slug is
the identifier the ATS uses for a company's board; the scraper combines it
with that platform's API or site URL to retrieve listings. The company lists
are JSON data, separate from the scraper code, so the set of configured
companies can be maintained independently.

## Collection and normalization

Each ATS has its own response format and retrieval method. The scraper modules
convert the data they receive into a common job record before writing it to
SQLite. Depending on the information an ATS provides, a record can include the
company, title, description, remote status, publication date, employment type,
job URL, compensation summary, and the time it was last scanned. Some fields
may be missing or unavailable for particular platforms.

Ashby, Lever, Greenhouse, and BambooHR retrieve listings from their platform
endpoints, process multiple company slugs concurrently, and collect the
resulting records for batch database writes. BambooHR also makes detail
requests for individual listings so it can retrieve the full posting
information. iCIMS follows a different path: it discovers job URLs through
company sitemaps and fetches each posting page to extract its description.

The Workday implementation is separate because its listings are paginated and
its endpoints may rate-limit requests. It retrieves pages for each company,
fetches details for previously unseen postings, and writes completed page
batches as it goes. This bounds how much job-description data needs to remain
in memory at once. Requests use per-host adaptive rate limiting; HTTP 429
responses cause the limiter to slow down, retry requests, and eventually give
up on a company slug if it continues to be rate-limited. The default runner
launches `Jobs.workday_alt` after all iCIMS chunks finish. This variant first
tries to parse job details from the public Workday job page's JSON-LD data and
falls back to the Workday detail API when that page data is unavailable.

## Database and duplicate handling

The local SQLite database is stored at `The_Database/job_data.db`. Its schema
has three core tables. The schema below reflects
`The_Database/sql_code/Table_Create.sql`.

### `companies` — configured employer boards

| Column | SQLite type | Rules | Description |
|---|---|---|---|
| `company_id` | `INTEGER` | Primary key | Internal identifier for a company. |
| `slug_name` | `TEXT` | Required; unique | Company slug used to identify its board on an ATS platform. |
| `created_at` | `TEXT` | Required; defaults to `CURRENT_TIMESTAMP` | Time the company row was created. |

### `sources` — scrape history

Each row records a scraper's result for one company and ATS. A company can have
many source rows over time, forming a history of scrape attempts.

| Column | SQLite type | Rules | Description |
|---|---|---|---|
| `source_id` | `INTEGER` | Primary key; included in `UNIQUE(source_id, company_id)` | Identifier for this scrape record. |
| `company_id` | `INTEGER` | Required; foreign key to `companies.company_id` | Company whose board was checked. Updates to the company ID cascade; deleting a referenced company is restricted. |
| `ats_vendor` | `TEXT` | Optional | ATS platform used for this scrape, such as Workday or Greenhouse. |
| `status` | `TEXT` | Optional | Outcome reported by the scraper, such as `success`, `fail`, `empty`, or `partial`. |
| `job_count` | `INTEGER` | Optional | Number of postings the scraper reported for this company. |
| `error` | `TEXT` | Optional | Error code or summary recorded when the scrape did not complete normally. |
| `created_at` | `TEXT` | Required; defaults to `CURRENT_TIMESTAMP` | Time the scrape-history row was created. |

### `job_postings` — collected job records

| Column | SQLite type | Rules | Description |
|---|---|---|---|
| `job_posting_id` | `INTEGER` | Primary key; auto-incrementing | Database identifier for the posting. |
| `company_id` | `INTEGER` | Required; foreign key to `companies.company_id` | Company associated with the posting. Updates to the company ID cascade; deleting a referenced company is restricted. |
| `job_title` | `TEXT` | Required | Posting title. |
| `job_remote` | `BOOLEAN` | Optional | Remote-work indicator when provided by the source. SQLite stores booleans using its integer representation. |
| `job_description` | `TEXT` | Required | Description text extracted from the posting. |
| `published_date` | `TEXT` | Optional | Publication date supplied or derived from the ATS. |
| `employment_type` | `TEXT` | Optional | Employment type or worker classification, when available. |
| `job_url` | `TEXT` | Optional; unique when non-NULL | Posting URL. The scraper uses this value to identify repeat sightings of a job. |
| `compensation_summary` | `TEXT` | Optional | Compensation information when available. |
| `last_scanned` | `DATE` | Optional | Most recent time the scraper saw this posting. This is a scan timestamp, not a guaranteed expiration or active-status field. |

### Relationships and constraints

```text
companies.company_id  1 ───────<  sources.company_id
companies.company_id  1 ───────<  job_postings.company_id
```

Company slugs and non-NULL job URLs are unique. The `sources` table also
declares a composite `UNIQUE(source_id, company_id)` constraint; since
`source_id` is already its primary key, this does not allow duplicate source
IDs. Foreign keys prevent deleting a company while source-history or
job-posting rows still reference it. The database schema does not define a
fixed allowed-value list for `status` or `ats_vendor`; those values are
supplied by the scraper code.

The posting URL is the deduplication key. When a URL is first encountered, the
scraper inserts a job row. If the same URL is encountered again, the database
keeps the existing job details and updates `last_scanned` to the latest scan
time. This timestamp indicates when the scraper last saw the listing; it is
not, by itself, proof that a posting is still accepting applications. Likewise,
the scraper does not currently delete or automatically hide postings that it
no longer finds.

Most scraper modules batch listing results and scrape logs for database writes.
The Workday module writes page-sized batches to keep memory use bounded.
Database writes are performed through the shared connection code, which uses
SQLite transactions and falls back to row-by-row insertion if a job batch
fails.

## Run order and existing data

When you run `Main.py`, it first creates the database directory if needed and
executes `The_Database/sql_code/Table_Create.sql`. The schema uses
`CREATE TABLE IF NOT EXISTS`, so this initializes a new database without
replacing existing tables or their contents. It then runs Ashby, Lever,
Greenhouse, and BambooHR in sequence, processes iCIMS company slugs in parallel
chunks, and runs Workday after iCIMS completes. If a scraper process fails, the
remaining modules are not started.

You can optionally begin with `job_data.db` from a GitHub release to search
previously collected postings without waiting for a full scrape. Without a
release database, the program creates an empty database and tables, then
populates them as the configured scrapers run.

# Get started in VS Code

1. Clone the repository:

   ```bash
   git clone https://github.com/EthanJG2814-Creator/Job-Scraper-Aggregator-Finder-Tool.git
   ```

2. In VS Code, select **File > Open Folder...** and open the cloned
   `Job-Scraper-Aggregator-Finder-Tool` folder. Open the integrated terminal
   with **Terminal > New Terminal**.

3. (Optional) Download the database ZIP from the repository's **Releases** page
   and extract `job-database-YYYY-MM-DD.db` into the `The_Database` folder. Make sure you rename it to `job_data.db` before you run the code. Keep a    backup before replacing an existing database.

4. In the VS Code terminal, create a virtual environment:

   ```bash
   python -m venv .venv
   ```

   Then open the Command Palette with **Ctrl+Shift+P** (Windows/Linux) or
   **Cmd+Shift+P** (macOS), run **Python: Select Interpreter**, and select the
   interpreter inside the `.venv` folder. If prompted, allow VS Code to reload
   the terminal so the environment is activated.

5. Install the dependencies in the VS Code terminal:

   ```bash
   python -m pip install -r requirements.txt
   ```

## Run the scraper and website

6. Start the scraper from the VS Code terminal:

   ```bash
   python Main.py
   ```

   The initial scrape can take a while. Keep this terminal open while the
   scraper runs. It adds new postings and updates records it encounters; it
   does not remove postings that are no longer listed on a job board.

7. After the scraper reports that the database has been initialized, open a
   second VS Code terminal and start the website. You can browse while the
   scraper continues running:

   ```bash
   python website/app.py
   ```

   Then open <http://127.0.0.1:5000/> in your browser. Stop the website with
   `Ctrl+C` when you are finished.

# Future updates

The following are planned improvements and are not available yet:

1. **Manage inactive postings.** The aggregator currently does not remove old
   or inactive postings from the website. A future update will hide postings
   that are no longer active while preserving them in the database for analysis.

2. **Add job categories and filters.** The website does not yet categorize
   postings by location, employment type, career level, or whether a role is
   intended for students. Future work will explore a combination of keyword
   matching and machine learning to classify postings and help users find
   relevant opportunities.

   Location classification will require extra care: companies use many
   different formats for locations, and each applicant-tracking system may
   structure location data differently. The goal is to normalize locations
   consistently, including locations around the world. Career-level
   classification (such as entry, intermediate, and senior) will also require
   testing and refinement, since job descriptions and employer labels are not
   always consistent.

3. **Expand coverage to hospital jobs.** Hospital roles are not currently
   included. A future scraper could collect hospital job listings across the
   United States. This will require adapting to the many different career-site
   platforms and website structures used by hospitals.

4. **Review inactive company listings.** Some companies in the initial dataset
   may have inactive links or outdated company slugs. Future work will improve
   how these companies are identified and determine whether they have moved
   to a new slug or are no longer available.

5. **Add architecture and database diagrams.** The README will include visual
   diagrams showing the planned end-to-end system architecture and the database
   schema, including how the scraper components, ATS platforms, and database
   tables relate to one another.

6. **Detect companies that have changed ATS platforms.** Some company slugs may
   stop working on their configured ATS because the company migrated to a
   different provider while keeping the same slug. A future detection
   mechanism will identify these cases and help route the company to the
   correct ATS scraper. Workday will be excluded from this automatic
   cross-platform detection.

# Disclaimer

This project is a job-search tool, not a comprehensive directory of every
employer or job opening. The database may include companies outside your
country, and some employers may be missing. Some companies post exclusively on
platforms that this project does not currently search, such as LinkedIn,
Glassdoor, or Handshake. As a result, search results may be incomplete or may
not reflect every available opening. Check an employer's official careers page
for the latest information.

# Issues and suggestions

If you find a bug, notice a company or job source that should be included, or
have an idea for improving the project, please open an issue in the repository's
[Issues](https://github.com/EthanJG2814-Creator/Job-Scraper-Aggregator-Finder-Tool/issues)
tab. Include enough detail to help reproduce the problem or understand your
suggestion.

# Acknowledgments

Thank you to [Riley Dorrington](https://github.com/Riley-Dorrington) for the
open-source [Job Board Aggregator project](https://github.com/Riley-Dorrington/job-board-aggregator).
It helped me understand parts of the project architecture, and I used its
company slug data as a reference when assembling the company lists for this
scraper. The scraping architecture and implementation in this project were
developed independently; I referred to Riley's project for guidance when I
needed it.
