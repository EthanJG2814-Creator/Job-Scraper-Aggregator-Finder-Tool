import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEBSITE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = WEBSITE_DIR.parent / "The_Database" / "job_data.db"
PAGE_SIZE = 25
ACTIVE_DAYS = 3
# Posting-age filter: value -> SQL condition on the effective posting date.
RECENT_DAYS = {"7d": 7, "14d": 14, "30d": 30}

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


_ats_cache = {"at": 0, "data": None}
ATS_CACHE_SECONDS = 300


def ats_stats():
    """Active postings per ATS vendor, using each company's latest source row."""
    if _ats_cache["data"] and time.time() - _ats_cache["at"] < ATS_CACHE_SECONDS:
        return _ats_cache["data"]
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            f"""SELECT COALESCE(s.ats_vendor, 'Unknown'), SUM(j.n)
                FROM (SELECT company_id, COUNT(*) AS n FROM job_postings j
                      WHERE {SCANNED} >= julianday('now', '-{ACTIVE_DAYS} days')
                      GROUP BY company_id) j
                JOIN sources s ON s.source_id =
                     (SELECT MAX(source_id) FROM sources WHERE company_id = j.company_id)
                GROUP BY 1 ORDER BY 2 DESC"""
        ).fetchall()
    total = sum(n for _, n in rows)
    data = {"total": total, "vendors": [{"name": v, "count": n} for v, n in rows]}
    _ats_cache.update(at=time.time(), data=data)
    return data


_timeline_cache = {"at": 0, "data": None}


def posting_timeline():
    """Postings per day per ATS vendor, from the oldest source scan date to today."""
    if _timeline_cache["data"] and time.time() - _timeline_cache["at"] < ATS_CACHE_SECONDS:
        return _timeline_cache["data"]
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(job_postings)")}
        posted = "NULLIF(published_date, '')"
        if "created_at" in columns:
            posted = f"COALESCE({posted}, created_at)"
        start = conn.execute("SELECT date(MIN(created_at)) FROM sources").fetchone()[0]
        end = conn.execute("SELECT date('now', 'localtime')").fetchone()[0]
        rows = conn.execute(
            f"""SELECT COALESCE(s.ats_vendor, 'Unknown'), d.day, SUM(d.n)
                FROM (SELECT company_id, date({posted}) AS day, COUNT(*) AS n
                      FROM job_postings WHERE {posted} IS NOT NULL
                      GROUP BY company_id, day) d
                JOIN sources s ON s.source_id =
                     (SELECT MAX(source_id) FROM sources WHERE company_id = d.company_id)
                WHERE d.day BETWEEN ? AND ?
                GROUP BY 1, 2""",
            (start, end),
        ).fetchall()
        days = [r[0] for r in conn.execute(
            """WITH RECURSIVE days(day) AS (
                   SELECT ? UNION ALL SELECT date(day, '+1 day') FROM days WHERE day < ?)
               SELECT day FROM days""", (start, end))] if start else []
    index = {day: i for i, day in enumerate(days)}
    series = {}
    for vendor, day, n in rows:
        series.setdefault(vendor, [0] * len(days))[index[day]] = n
    # Days where every vendor is 0 were never logged, so they are left out.
    keep = [i for i in range(len(days)) if any(c[i] for c in series.values())]
    data = {
        "dates": [days[i] for i in keep],
        "series": [{"name": v, "counts": [c[i] for i in keep]} for v, c in series.items()],
    }
    _timeline_cache.update(at=time.time(), data=data)
    return data


def escape_like(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_jobs(params):
    title = params.get("title", [""])[0].strip()
    description = params.get("description", [""])[0]
    sort = params.get("sort", ["mixed"])[0]
    recent = params.get("recent", ["all"])[0]
    try:
        page = max(1, int(params.get("page", ["1"])[0]))
    except ValueError:
        page = 1

    # Jobs not seen in a scan within the last 3 days are considered inactive.
    where, args = [f"{SCANNED} >= julianday('now', '-{ACTIVE_DAYS} days')"], []
    if title:
        where.append("j.job_title LIKE ? ESCAPE '\\'")
        args.append(f"%{escape_like(title)}%")
    for term in (t.strip() for t in description.split(",")):
        if term:
            where.append("j.job_description LIKE ? ESCAPE '\\'")
            args.append(f"%{escape_like(term)}%")
    order = SORT_ORDERS.get(sort, SORT_ORDERS["mixed"])

    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        # Posting date falls back to created_at; until the column exists in the
        # database, only published_date is used.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(job_postings)")}
        posted = "NULLIF(j.published_date, '')"
        if "created_at" in columns:
            posted = f"COALESCE({posted}, j.created_at)"
        if recent == "today":
            where.append(f"date({posted}, 'localtime') = date('now', 'localtime')")
        elif recent in RECENT_DAYS:
            where.append(f"julianday({posted}) >= julianday('now', '-{RECENT_DAYS[recent]} days')")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
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
        elif url.path == "/analyst":
            self.send_bytes(200, (WEBSITE_DIR / "analyst.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/static/analyst.js":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "analyst.js").read_bytes(), "text/javascript")
        elif url.path == "/api/ats-stats":
            try:
                self.send_bytes(200, json.dumps(ats_stats()).encode(), "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path == "/api/ats-timeline":
            try:
                self.send_bytes(200, json.dumps(posting_timeline()).encode(), "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
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
