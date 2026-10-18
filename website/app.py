import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEBSITE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = WEBSITE_DIR.parent / "The_Database" / "job_data.db"
PAGE_SIZE = 25

# Published date and last scanned are ISO-8601 strings in mixed formats,
# so julianday() normalizes them. Missing published dates become NULL.
PUBLISHED = "julianday(NULLIF(j.published_date, ''))"
SCANNED = "julianday(NULLIF(j.last_scanned, ''))"

SORT_ORDERS = {
    # Published jobs first, newest first; unpublished jobs go to the end.
    "published": f"{PUBLISHED} IS NULL, {PUBLISHED} DESC, {SCANNED} DESC",
    "scanned": f"{SCANNED} IS NULL, {SCANNED} DESC",
    # Published day descending, then last scanned descending within the same day;
    # unpublished jobs follow, ordered by last scanned.
    "mixed": f"{PUBLISHED} IS NULL, date(j.published_date) DESC, {SCANNED} DESC",
}


def escape_like(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_jobs(params):
    title = params.get("title", [""])[0].strip()
    description = params.get("description", [""])[0]
    sort = params.get("sort", ["mixed"])[0]
    try:
        page = max(1, int(params.get("page", ["1"])[0]))
    except ValueError:
        page = 1

    where, args = [], []
    if title:
        where.append("j.job_title LIKE ? ESCAPE '\\'")
        args.append(f"%{escape_like(title)}%")
    for term in (t.strip() for t in description.split(",")):
        if term:
            where.append("j.job_description LIKE ? ESCAPE '\\'")
            args.append(f"%{escape_like(term)}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    order = SORT_ORDERS.get(sort, SORT_ORDERS["mixed"])

    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute(f"SELECT COUNT(*) FROM job_postings j {clause}", args).fetchone()[0]
        rows = conn.execute(
            f"""SELECT j.job_posting_id AS id, s.ats_vendor, j.job_title AS title, c.slug_name AS company,
                       j.published_date, j.last_scanned, j.employment_type, j.job_remote,
                       j.compensation_summary, j.job_url, j.job_description AS description
                FROM job_postings j LEFT JOIN companies c ON c.company_id = j.company_id
                LEFT JOIN sources s ON s.company_id = j.company_id
                {clause} ORDER BY {order}, j.job_posting_id DESC LIMIT ? OFFSET ?""",
            args + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
    return {
        "total": total,
        "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "jobs": [dict(r) for r in rows],
    }


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send_bytes(200, (WEBSITE_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/static/styles.css":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "styles.css").read_bytes(), "text/css")
        elif url.path == "/static/app.js":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "app.js").read_bytes(), "text/javascript")
        elif url.path == "/api/jobs":
            try:
                body = json.dumps(search_jobs(parse_qs(url.query))).encode()
                self.send_bytes(200, body, "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        else:
            self.send_bytes(404, b"Not found", "text/plain")


if __name__ == "__main__":
    print("Serving on http://127.0.0.1:5000/")
    ThreadingHTTPServer(("127.0.0.1", 5000), Handler).serve_forever()
