import json
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pycountry

WEBSITE_DIR = Path(__file__).resolve().parent
MATRIX_DIR = WEBSITE_DIR.parent / "Job Matrix"
_matrix = {"store": None, "mtime": None}
RESUME_PROFILES_PATH = WEBSITE_DIR.parent / "Resume_Matcher" / "data" / "profiles.json"
_resume_lock = threading.Lock()
TITLE_INDEX_SCRIPT = WEBSITE_DIR.parent / "Resume_Matcher" / "title_index.py"
TITLE_INDEX_PATH = WEBSITE_DIR.parent / "Resume_Matcher" / "data" / "title_embeddings.sqlite3"
TITLE_INDEX_LOG = WEBSITE_DIR.parent / "Resume_Matcher" / "data" / "title_index.log"
CLUSTER_SCRIPT = WEBSITE_DIR.parent / "Resume_Matcher" / "cluster_matching.py"
_title_lock = threading.Lock()
_title_index_cache = {"stamp": None, "data": None}
_title_query_cache = {}
_title_builder = None
_cluster_lock = threading.Lock()
_cluster_positions_cache = {"key": None, "data": None}
_experience_embedding_cache = {}


class ResumeConflict(Exception):
    """A candidate profile was changed since it was loaded."""


def load_resume_profiles():
    try:
        with RESUME_PROFILES_PATH.open(encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as error:
        raise OSError("The candidate profiles file contains invalid JSON.") from error
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        raise OSError("The candidate profiles file has an invalid format.")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or type(item.get("version")) is not int
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("experiences"), list)
        for item in data["profiles"]
    ):
        raise OSError("The candidate profiles file has an invalid format.")
    if len({item["id"] for item in data["profiles"]}) != len(data["profiles"]):
        raise OSError("The candidate profiles file has duplicate IDs.")
    try:
        for item in data["profiles"]:
            validate_resume_profile(item)
    except ValueError as error:
        raise OSError(f"The candidate profiles file has invalid data: {error}") from error
    return data["profiles"]


def save_resume_profiles(profiles):
    folder = RESUME_PROFILES_PATH.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=folder, prefix=".profiles-", suffix=".tmp",
            delete=False,
        ) as file:
            temp_path = Path(file.name)
            json.dump({"profiles": profiles}, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_path, RESUME_PROFILES_PATH)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def validate_resume_profile(data):
    if not isinstance(data, dict):
        raise ValueError("A candidate profile is required.")
    name = data.get("name")
    experiences = data.get("experiences")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError("Candidate name is required and must be at most 200 characters.")
    if not isinstance(experiences, list) or len(experiences) > 200:
        raise ValueError("Work experience must be a list of at most 200 entries.")
    cleaned = []
    for experience in experiences:
        if not isinstance(experience, dict):
            raise ValueError("Invalid work experience entry.")
        values = {}
        for field, limit in (("title", 200), ("company", 200), ("start", 7), ("end", 7), ("description", 10000)):
            value = experience.get(field, "")
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"Invalid {field} in work experience.")
            values[field] = value.strip()
        current = experience.get("current", False)
        if not isinstance(current, bool):
            raise ValueError("Current job must be true or false.")
        if not values["title"]:
            raise ValueError("Each work experience entry requires a job title.")
        for field in ("start", "end"):
            if values[field] and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", values[field]):
                raise ValueError("Dates must use the YYYY-MM format.")
        if current and values["end"]:
            raise ValueError("A current job cannot have an end date.")
        if values["start"] and values["end"] and values["end"] < values["start"]:
            raise ValueError("End date must not be before start date.")
        values["current"] = current
        cleaned.append(values)
    return {"name": name.strip(), "experiences": cleaned}


def change_resume_profile(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid request.")
    action = data.get("action")
    if action not in ("create", "update", "delete"):
        raise ValueError("Unknown profile action.")
    with _resume_lock:
        profiles = load_resume_profiles()
        if action == "create":
            profile = validate_resume_profile(data.get("profile"))
            profile.update(id=uuid.uuid4().hex, version=1)
            profiles.append(profile)
        else:
            profile = next((item for item in profiles if item.get("id") == data.get("id")), None)
            if profile is None:
                raise ValueError("Candidate profile not found.")
            if type(data.get("version")) is not int or data["version"] != profile["version"]:
                raise ResumeConflict("This profile changed in another tab. Reload before saving.")
            if action == "delete":
                profiles.remove(profile)
            else:
                updated = validate_resume_profile(data.get("profile"))
                profile.update(updated)
                profile["version"] += 1
        save_resume_profiles(profiles)
        return {"profiles": profiles, "id": profile["id"]}


class TitleIndexUnavailable(Exception):
    """The title index is missing or incomplete."""


def title_index_module():
    folder = str(TITLE_INDEX_SCRIPT.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    import title_index
    return title_index


def title_model_python():
    configured = os.environ.get("RESUME_MATCHER_PYTHON")
    if configured:
        return configured
    venv = WEBSITE_DIR.parent.parent / "embedding models" / ".venv"
    return next(
        (str(path) for path in (venv / "bin" / "python", venv / "Scripts" / "python.exe") if path.is_file()),
        sys.executable,
    )


def start_title_index():
    global _title_builder
    with _title_lock:
        if _title_builder is not None and _title_builder.poll() is None:
            return {"status": "building", "message": "Title index build is already running."}
        TITLE_INDEX_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TITLE_INDEX_LOG.open("w", encoding="utf-8") as log:
            _title_builder = subprocess.Popen(
                [title_model_python(), "-u", str(TITLE_INDEX_SCRIPT), "build"],
                stdout=log, stderr=subprocess.STDOUT,
            )
        return {"status": "building", "message": "Title index build started."}


def title_index_status():
    with _title_lock:
        if _title_builder is not None:
            code = _title_builder.poll()
            status = "building" if code is None else "ready" if code == 0 else "failed"
            message = ""
            if TITLE_INDEX_LOG.exists():
                with TITLE_INDEX_LOG.open("rb") as file:
                    file.seek(0, os.SEEK_END)
                    file.seek(max(0, file.tell() - 4096))
                    lines = file.read().decode("utf-8", errors="replace").splitlines()
                    message = lines[-1] if lines else ""
            return {"status": status, "message": message}
        if TITLE_INDEX_PATH.is_file():
            return {"status": "available", "message": "Update the index when new job titles are added."}
        return {"status": "missing", "message": "Build the title index before searching."}


def load_title_index():
    import numpy as np

    if not TITLE_INDEX_PATH.is_file():
        raise TitleIndexUnavailable("Title index not found. Build the title index first.")
    stamp = (TITLE_INDEX_PATH.stat().st_mtime_ns, TITLE_INDEX_PATH.stat().st_size)
    with _title_lock:
        if _title_index_cache["stamp"] == stamp:
            return _title_index_cache["data"]
        model = title_index_module()
        with sqlite3.connect(f"file:{TITLE_INDEX_PATH}?mode=ro", uri=True) as conn:
            conn.execute("BEGIN")
            metadata = dict(conn.execute("SELECT key, value FROM metadata"))
            if metadata != {"model": model.MODEL_KEY, "dimensions": str(model.DIMENSIONS)}:
                raise OSError("Title index model or format is incompatible.")
            count = conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
            vectors = np.empty((count, model.DIMENSIONS), dtype=np.float32)
            lookup = {}
            for index, (title, raw) in enumerate(conn.execute("SELECT title, embedding FROM titles ORDER BY rowid")):
                if not isinstance(title, str) or not isinstance(raw, bytes) or len(raw) != model.DIMENSIONS * 2:
                    raise OSError("Title index contains invalid embeddings.")
                lookup[title] = index
                vectors[index] = np.frombuffer(raw, dtype="<f2")
        norms = np.linalg.norm(vectors, axis=1)
        if not np.isfinite(norms).all() or np.any(norms == 0):
            raise OSError("Title index contains invalid embeddings.")
        vectors /= norms[:, None]
        data = (lookup, vectors)
        _title_index_cache.update(stamp=stamp, data=data)
        _title_query_cache.clear()
        return data


def score_title_index(titles, vectors):
    import numpy as np

    key = (id(vectors), *titles)
    with _title_lock:
        if key in _title_query_cache:
            return _title_query_cache[key][1:]
        env = os.environ.copy()
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        try:
            result = subprocess.run(
                [title_model_python(), str(TITLE_INDEX_SCRIPT), "encode"],
                input=json.dumps(titles), capture_output=True, text=True, env=env, timeout=180, check=True,
            )
        except subprocess.CalledProcessError as error:
            raise OSError(f"Could not load the local title model: {error.stderr[-1200:]}") from error
        except subprocess.TimeoutExpired as error:
            raise OSError("The title model timed out.") from error
        try:
            queries = np.asarray(json.loads(result.stdout), dtype=np.float32)
        except (ValueError, TypeError) as error:
            raise OSError("The title model returned invalid output.") from error
        if queries.shape != (len(titles), vectors.shape[1]) or not np.isfinite(queries).all():
            raise OSError("The title model returned invalid vectors.")
        norms = np.linalg.norm(queries, axis=1)
        if np.any(norms == 0):
            raise OSError("The title model returned a zero vector.")
        queries /= norms[:, None]
        scores = np.full(len(vectors), -np.inf, dtype=np.float32)
        best = np.zeros(len(vectors), dtype=np.int32)
        exact = []
        for i, query in enumerate(queries):
            candidate = np.einsum("ij,j->i", vectors, query)
            exact.append(np.flatnonzero(candidate >= 1 - 1e-6))
            better = candidate > scores
            scores[better] = candidate[better]
            best[better] = i
        if len(_title_query_cache) >= 8:
            _title_query_cache.pop(next(iter(_title_query_cache)))
        _title_query_cache[key] = (vectors, scores, best, exact)
        return scores, best, exact


def run_cluster_worker(command, *, folder=None, descriptions=None):
    env = os.environ.copy()
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OPENBLAS_NUM_THREADS="2", OMP_NUM_THREADS="2")
    python = os.environ.get("JOB_MATRIX_PYTHON") or title_model_python()
    args = [python, str(CLUSTER_SCRIPT), command]
    if folder is not None:
        args += ["--matrix", str(folder), "--index", str(TITLE_INDEX_PATH)]
    try:
        result = subprocess.run(
            args, input=json.dumps(descriptions).encode() if descriptions is not None else None,
            capture_output=True, env=env, timeout=600, check=True,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace")[-1500:]
        raise OSError(f"Could not calculate matrix positions: {detail}") from error
    except subprocess.TimeoutExpired as error:
        raise OSError("Calculating matrix positions timed out.") from error
    return io.BytesIO(result.stdout)


def cluster_title_positions(store, vectors):
    import numpy as np

    folder = Path(_matrix["mtime"][0])
    if json.loads((folder / "meta.json").read_text(encoding="utf-8"))["version"] != store.version:
        raise TitleIndexUnavailable("The Job Matrix changed; retry the search.")
    stamp = (TITLE_INDEX_PATH.stat().st_mtime_ns, TITLE_INDEX_PATH.stat().st_size)
    key = (store.version, str(folder), stamp, id(vectors))
    with _cluster_lock:
        if _cluster_positions_cache["key"] == key:
            return _cluster_positions_cache["data"]
        if _title_index_cache["stamp"] != stamp or _title_index_cache["data"][1] is not vectors:
            raise TitleIndexUnavailable("The title index changed; retry the search.")
        data = run_cluster_worker("title-positions", folder=folder)
        try:
            positions = np.load(data, allow_pickle=False)
            estimated = np.load(data, allow_pickle=False)
        except (ValueError, EOFError) as error:
            raise OSError("The matrix placement worker returned invalid data.") from error
        if (
            positions.shape != (len(vectors), 3) or estimated.shape != (len(vectors),)
            or estimated.dtype != np.bool_ or not np.isfinite(positions).all()
        ):
            raise OSError("The matrix placement worker returned invalid positions.")
        if stamp != (TITLE_INDEX_PATH.stat().st_mtime_ns, TITLE_INDEX_PATH.stat().st_size):
            raise TitleIndexUnavailable("The title index changed while placing jobs; retry the search.")
        _cluster_positions_cache.update(key=key, data=(positions, estimated))
        return positions, estimated


def cluster_experience_embeddings(descriptions):
    import numpy as np

    key = tuple(descriptions)
    with _cluster_lock:
        if key in _experience_embedding_cache:
            return _experience_embedding_cache[key]
        data = run_cluster_worker("experience-embeddings", descriptions=descriptions)
        try:
            vectors = np.load(data, allow_pickle=False)
        except (ValueError, EOFError) as error:
            raise OSError("The matrix model returned invalid embeddings.") from error
        if vectors.ndim != 2 or len(vectors) != len(descriptions) or not np.isfinite(vectors).all():
            raise OSError("The matrix model returned invalid embeddings.")
        if len(_experience_embedding_cache) >= 8:
            _experience_embedding_cache.pop(next(iter(_experience_embedding_cache)))
        _experience_embedding_cache[key] = vectors
        return vectors


def cluster_scores(conn, profile, titles, exact, lookup, title_positions, store):
    import numpy as np

    names = [None] * len(lookup)
    for title, index in lookup.items():
        names[index] = title
    exact_by_title = dict(zip(titles, exact))
    possible = {names[int(index)] for indexes in exact for index in indexes}
    counts = {}
    possible = list(possible)
    for start in range(0, len(possible), 500):
        batch = possible[start:start + 500]
        rows = conn.execute(
            f"SELECT job_title, COUNT(*) FROM job_postings WHERE job_title IN ({','.join('?' for _ in batch)}) GROUP BY job_title",
            batch,
        )
        counts.update(rows)
    anchors = [None] * len(profile["experiences"])
    descriptions = []
    missing = []
    for i, experience in enumerate(profile["experiences"]):
        title = experience["title"]
        indexes = [int(index) for index in exact_by_title[title] if names[int(index)] in counts]
        if indexes:
            positions = np.repeat(title_positions[indexes], [counts[names[index]] for index in indexes], axis=0)
            anchors[i] = np.median(positions, axis=0)
        else:
            description = experience.get("description", "").strip()
            if not description:
                raise ValueError(f"Add a work description for '{title}' to place it on the Job Matrix.")
            missing.append(i)
            descriptions.append(description)
    if missing:
        folder = str(CLUSTER_SCRIPT.parent)
        if folder not in sys.path:
            sys.path.insert(0, folder)
        from cluster_matching import interpolate_positions
        vectors = cluster_experience_embeddings(descriptions)
        placed = interpolate_positions(vectors, store._embeddings, store.pos)
        for i, position in zip(missing, placed):
            anchors[i] = position
    anchors = np.asarray(anchors, dtype=np.float32)
    center = np.median(store.pos, axis=0)
    scale = float(np.median(np.linalg.norm(store.pos - center, axis=1)))
    if not np.isfinite(scale) or scale <= 0:
        raise OSError("The Job Matrix does not have enough distinct positions for distance scoring.")
    scores = np.zeros(len(title_positions), dtype=np.float32)
    closest = np.full(len(title_positions), -np.inf, dtype=np.float32)
    best = np.zeros(len(title_positions), dtype=np.int32)
    distances = np.zeros(len(title_positions), dtype=np.float32)
    for i, anchor in enumerate(anchors):
        distance = np.linalg.norm(title_positions - anchor, axis=1)
        similarity = 1.0 / (1.0 + distance / scale)
        scores += similarity
        better = similarity > closest
        closest[better], best[better], distances[better] = similarity[better], i, distance[better]
    return scores / len(anchors), best, distances


class MatrixStale(Exception):
    """The matrix was updated since this page loaded it."""


def matrix_store():
    """Job Matrix data, loaded on first use and reloaded when job_matrix.py publishes a new version."""
    import sys
    if str(MATRIX_DIR) not in sys.path:
        sys.path.insert(0, str(MATRIX_DIR))
    from matrix_store import current_data_dir
    folder = current_data_dir()
    meta = folder / "meta.json"
    if not meta.exists():
        raise FileNotFoundError('Job Matrix data not built. Run: python "Job Matrix/job_matrix.py" update')
    stamp = (str(folder), meta.stat().st_mtime)
    if _matrix["store"] is None or _matrix["mtime"] != stamp:
        from matrix_store import MatrixStore
        _matrix["store"], _matrix["mtime"] = MatrixStore(folder), stamp
    return _matrix["store"]


def matrix_api(path, params):
    store = matrix_store()
    # Pages send the version they loaded; ids from an older version would point at the wrong jobs.
    version = params.get("v", [""])[0]
    if version and version != store.version and path != "/api/matrix/graph":
        raise MatrixStale("The Job Matrix was updated")
    # countries=US,CA,?? limits everything to jobs seen in those countries; omitted means all.
    requested = {code.strip().upper() for code in params.get("countries", [""])[0].split(",") if code.strip()}
    chosen = store.selection(requested)
    if path == "/api/matrix/graph":
        return store.graph_payload()
    if path == "/api/matrix/countries":
        return store.country_list()
    if path == "/api/matrix/filter":
        active = store.active_mask(chosen)
        return {"all": chosen is None, "count": int(active.sum()),
                "active": None if chosen is None else [i for i, on in enumerate(active) if on]}
    if path == "/api/matrix/search":
        query = params.get("q", [""])[0].strip()
        return {"results": store.search(query, 12, chosen) if query else []}
    try:
        if path == "/api/matrix/node":
            node = int(params.get("id", [""])[0])
        else:
            src, dst = int(params.get("from", [""])[0]), int(params.get("to", [""])[0])
    except ValueError:
        raise ValueError("numeric node ids required")
    if path == "/api/matrix/node":
        if not 0 <= node < store.n:
            raise ValueError("unknown node")
        return store.detail(node, chosen=chosen)
    if not (0 <= src < store.n and 0 <= dst < store.n):
        raise ValueError("unknown node")
    return store.paths(src, dst, chosen)

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


def top_cities():
    """Top 25 cities by job posting count (both US and foreign)."""
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        rows = conn.execute("""
            SELECT city_name, state_abbv, country, COUNT(*) as count
            FROM locations
            WHERE city_name IS NOT NULL AND city_name != ''
            AND country IS NOT NULL AND country != ''
            GROUP BY city_name, state_abbv, country
            ORDER BY count DESC
            LIMIT 25
        """).fetchall()
    return {"cities": [{"city": r[0], "state": r[1], "country": r[2], "count": r[3]} for r in rows]}


def top_us_states():
    """Top 10 US states by job posting count."""
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        rows = conn.execute("""
            SELECT state_abbv, country, COUNT(*) as count
            FROM locations
            WHERE state_abbv IS NOT NULL AND state_abbv != ''
            AND country = 'US'
            GROUP BY state_abbv, country
            ORDER BY count DESC
            LIMIT 10
        """).fetchall()
    return {"states": [{"state": r[0], "country": r[1], "count": r[2]} for r in rows]}


def top_countries():
    """Top 3 countries by job posting count."""
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        rows = conn.execute("""
            SELECT country, COUNT(*) as count
            FROM locations
            WHERE country IS NOT NULL AND country != ''
            GROUP BY country
            ORDER BY count DESC
            LIMIT 3
        """).fetchall()
    countries = []
    for code, count in rows:
        try:
            country_obj = pycountry.countries.get(alpha_2=code)
            name = country_obj.name if country_obj else code
        except (AttributeError, KeyError):
            name = code
        countries.append({"code": code, "name": name, "count": count})
    return {"countries": countries}


def add_bookmark(session_id, job_id):
    """Add a bookmark for a job posting. Returns success status."""
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        # Verify job exists
        if not conn.execute("SELECT 1 FROM job_postings WHERE job_posting_id = ?", (job_id,)).fetchone():
            raise ValueError("Job posting not found")
    
    with sqlite3.connect(DATABASE_PATH) as conn:
        try:
            conn.execute(
                "INSERT INTO bookmarks (job_posting_id, session_id) VALUES (?, ?)",
                (job_id, session_id)
            )
            conn.commit()
            return {"success": True, "message": "Bookmark added"}
        except sqlite3.IntegrityError:
            return {"success": False, "message": "Already bookmarked"}


def remove_bookmark(session_id, job_id):
    """Remove a bookmark. Returns success status."""
    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM bookmarks WHERE job_posting_id = ? AND session_id = ?",
            (job_id, session_id)
        )
        conn.commit()
        return {"success": cursor.rowcount > 0, "message": "Bookmark removed" if cursor.rowcount > 0 else "Bookmark not found"}


def get_bookmarks(session_id, page=1):
    """Get bookmarks for a session with full job details."""
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        
        # Get total count
        total = conn.execute(
            "SELECT COUNT(*) FROM bookmarks WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        
        # Get bookmarked jobs with details
        rows = conn.execute(f"""
            SELECT j.job_posting_id AS id, s.ats_vendor, j.job_title AS title, c.slug_name AS company,
                   j.published_date, j.last_scanned, j.employment_type, j.job_remote,
                   j.compensation_summary, j.job_url, j.job_description AS description,
                   b.created_at AS bookmarked_at
            FROM bookmarks b
            JOIN job_postings j ON j.job_posting_id = b.job_posting_id
            LEFT JOIN companies c ON c.company_id = j.company_id
            LEFT JOIN sources s ON s.source_id =
                 (SELECT MAX(source_id) FROM sources WHERE company_id = j.company_id)
            WHERE b.session_id = ?
            ORDER BY b.created_at DESC
            LIMIT ? OFFSET ?
        """, (session_id, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
        
        jobs = [dict(row) for row in rows]
        
        # Get locations for these jobs
        has_locations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='locations'"
        ).fetchone()
        if has_locations and jobs:
            ids = [job["id"] for job in jobs]
            location_columns = {row["name"] for row in conn.execute("PRAGMA table_info(locations)")}
            remote = "is_remote" if "is_remote" in location_columns else "0 AS is_remote"
            hybrid = "is_hybrid" if "is_hybrid" in location_columns else "0 AS is_hybrid"
            location_rows = conn.execute(
                f"""SELECT DISTINCT job_posting_id, city_name, state_abbv, country, {remote}, {hybrid}
                    FROM locations WHERE job_posting_id IN ({','.join('?' for _ in ids)})
                    ORDER BY country, state_abbv, city_name""", ids
            ).fetchall()
            by_job = {}
            for row in location_rows:
                by_job.setdefault(row["job_posting_id"], []).append({
                    name: row[name] for name in ("city_name", "state_abbv", "country", "is_remote", "is_hybrid")
                })
            for job in jobs:
                job["locations"] = by_job.get(job["id"], [])
        else:
            for job in jobs:
                job["locations"] = []
    
    return {
        "total": total,
        "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "jobs": jobs,
    }


def is_bookmarked(session_id, job_id):
    """Check if a job is bookmarked by the session."""
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        result = conn.execute(
            "SELECT 1 FROM bookmarks WHERE job_posting_id = ? AND session_id = ?",
            (job_id, session_id)
        ).fetchone()
        return {"bookmarked": bool(result)}


def location_options():
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='locations'").fetchone():
            return {"available": False, "countries": []}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(locations)")}
        if not {"country", "state_abbv", "city_name"} <= columns:
            raise sqlite3.DatabaseError("The locations table is missing required columns")
        rows = conn.execute("""
            SELECT DISTINCT country, state_abbv, city_name FROM locations
            WHERE country IS NOT NULL AND country != ''
            ORDER BY country, state_abbv, city_name
        """).fetchall()
    countries = {}
    state_names = {subdivision.code: subdivision.name for subdivision in pycountry.subdivisions
                   if subdivision.country_code in ("US", "CA")}
    for country, state, city in rows:
        item = countries.setdefault(country, {"code": country, "states": {}, "state_names": {}, "cities": []})
        if state:
            cities = item["states"].setdefault(state, [])
            item["state_names"][state] = state_names.get(f"{country}-{state}", state)
            if city:
                cities.append(city)
        elif city:
            item["cities"].append(city)
    return {"available": True, "countries": list(countries.values())}


def location_condition(value):
    try:
        selections = json.loads(value)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid location filter JSON") from error
    if not isinstance(selections, list) or not selections:
        raise ValueError("Location filter must be a non-empty list")
    clauses, args = [], []
    for selection in selections:
        if not isinstance(selection, dict):
            raise ValueError("Invalid country selection")
        country = selection.get("country")
        states = selection.get("states", {})
        cities = selection.get("cities", [])
        if (not isinstance(country, str) or not country.strip()
                or not isinstance(states, dict) or not isinstance(cities, list)
                or (states and cities)):
            raise ValueError("Invalid country, state, or city selection")
        if any(not isinstance(city, str) or not city.strip() for city in cities):
            raise ValueError("Invalid city selection")
        parts = ["l.country = ?"]
        args.append(country)
        state_clauses = []
        for state, state_cities in states.items():
            if (not isinstance(state, str) or not state.strip()
                    or not isinstance(state_cities, list)
                    or any(not isinstance(city, str) or not city.strip() for city in state_cities)):
                raise ValueError("Invalid state or city selection")
            state_clause = "l.state_abbv = ?"
            args.append(state)
            if state_cities:
                state_clause += f" AND l.city_name IN ({','.join('?' for _ in state_cities)})"
                args.extend(state_cities)
            state_clauses.append(f"({state_clause})")
        if state_clauses:
            parts.append(f"({' OR '.join(state_clauses)})")
        elif cities:
            parts.append(f"l.city_name IN ({','.join('?' for _ in cities)})")
            args.extend(cities)
        clauses.append(f"({' AND '.join(parts)})")
    return "j.job_posting_id IN (SELECT l.job_posting_id FROM locations l WHERE " + " OR ".join(clauses) + ")", args


def search_jobs(params):
    title = params.get("title", [""])[0].strip()
    description = params.get("description", [""])[0]
    sort = params.get("sort", ["mixed"])[0]
    recent = params.get("recent", ["all"])[0]
    vendor = params.get("vendor", ["all"])[0]
    location = params.get("location", [""])[0]
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
        if location:
            condition, location_args = location_condition(location)
            where.append(condition)
            args.extend(location_args)
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
        if vendor != "all":
            ids = [r[0] for r in conn.execute(
                """SELECT s.company_id FROM sources s
                   JOIN (SELECT MAX(source_id) AS sid FROM sources GROUP BY company_id) m
                     ON s.source_id = m.sid
                   WHERE s.ats_vendor = ?""",
                (vendor,),
            )]
            where.append(f"j.company_id IN ({','.join(str(int(i)) for i in ids) or 'NULL'})")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM job_postings j {clause}", args).fetchone()[0]
        # Page the postings first, then look up each page row's latest source;
        # joining before sorting made the unfiltered query far too slow.
        rows = conn.execute(
            f"""SELECT j.job_posting_id AS id, s.ats_vendor, j.job_title AS title, c.slug_name AS company,
                       j.published_date, j.last_scanned, j.employment_type, j.job_remote,
                       j.compensation_summary, j.job_url, j.job_description AS description
                FROM (SELECT j.job_posting_id AS pid FROM job_postings j {clause}
                      ORDER BY {order}, j.job_posting_id DESC LIMIT ? OFFSET ?) p
                JOIN job_postings j ON j.job_posting_id = p.pid
                LEFT JOIN companies c ON c.company_id = j.company_id
                LEFT JOIN sources s ON s.source_id =
                     (SELECT MAX(source_id) FROM sources WHERE company_id = j.company_id)
                ORDER BY {order}, j.job_posting_id DESC""",
            args + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        jobs = [dict(row) for row in rows]
        has_locations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='locations'"
        ).fetchone()
        if has_locations and jobs:
            ids = [job["id"] for job in jobs]
            location_columns = {row["name"] for row in conn.execute("PRAGMA table_info(locations)")}
            remote = "is_remote" if "is_remote" in location_columns else "0 AS is_remote"
            hybrid = "is_hybrid" if "is_hybrid" in location_columns else "0 AS is_hybrid"
            location_rows = conn.execute(
                f"""SELECT DISTINCT job_posting_id, city_name, state_abbv, country, {remote}, {hybrid}
                    FROM locations WHERE job_posting_id IN ({','.join('?' for _ in ids)})
                    ORDER BY country, state_abbv, city_name""", ids
            ).fetchall()
            by_job = {}
            for row in location_rows:
                by_job.setdefault(row["job_posting_id"], []).append({
                    name: row[name] for name in ("city_name", "state_abbv", "country", "is_remote", "is_hybrid")
                })
            for job in jobs:
                job["locations"] = by_job.get(job["id"], [])
        else:
            for job in jobs:
                job["locations"] = []
    return {
        "total": total,
        "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "jobs": jobs,
    }


def match_resume_jobs(params):
    import numpy as np

    profile_id = params.get("profile", [""])[0]
    with _resume_lock:
        profile = next((item for item in load_resume_profiles() if item["id"] == profile_id), None)
    if profile is None:
        raise ValueError("Select an existing candidate profile.")
    titles = list(dict.fromkeys(entry["title"] for entry in profile["experiences"] if entry["title"].strip()))
    if not titles:
        raise ValueError("Add at least one work experience title to this profile.")
    try:
        page = max(1, int(params.get("page", ["1"])[0]))
    except ValueError as error:
        raise ValueError("Page must be a positive number.") from error
    where, args = "", []
    location = params.get("location", [""])[0]
    if location:
        condition, args = location_condition(location)
        where = f"WHERE {condition}"

    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        rows = conn.execute(f"SELECT j.job_posting_id, j.job_title FROM job_postings j {where}", args).fetchall()
        total = len(rows)
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(page, pages)
        if not rows:
            return {"total": 0, "page": page, "pages": pages, "jobs": []}
        lookup, vectors = load_title_index()
        indexes = []
        missing = set()
        for row in rows:
            title = row["job_title"]
            if not title or not title.strip():
                indexes.append(-1)
            elif title not in lookup:
                missing.add(title)
                indexes.append(-1)
            else:
                indexes.append(lookup[title])
        if missing:
            raise TitleIndexUnavailable(
                f"Title index is missing {len(missing):,} titles in this search. Update the title index."
            )
        title_scores, best, exact = score_title_index(titles, vectors)
        store = matrix_store()
        positions, estimated = cluster_title_positions(store, vectors)
        cluster, cluster_best, distances = cluster_scores(conn, profile, titles, exact, lookup, positions, store)
        title_indexes = np.asarray(indexes, dtype=np.int64)
        row_scores = np.full(total, -np.inf, dtype=np.float32)
        valid = title_indexes >= 0
        row_scores[valid] = (
            0.5 * np.clip(title_scores[title_indexes[valid]], -1, 1)
            + 0.5 * cluster[title_indexes[valid]]
        )
        ids = np.asarray([row["job_posting_id"] for row in rows], dtype=np.int64)
        order = np.lexsort((-ids, -row_scores))
        chosen = order[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        selected_ids = ids[chosen].tolist()
        job_rows = conn.execute(
            f"""SELECT j.job_posting_id AS id, j.job_title AS title, c.slug_name AS company,
                       j.published_date, j.last_scanned, j.employment_type, j.job_remote,
                       j.compensation_summary, j.job_url, j.job_description AS description, s.ats_vendor
                FROM job_postings j LEFT JOIN companies c ON c.company_id = j.company_id
                LEFT JOIN sources s ON s.source_id =
                    (SELECT MAX(source_id) FROM sources WHERE company_id = j.company_id)
                WHERE j.job_posting_id IN ({','.join('?' for _ in selected_ids)})""",
            selected_ids,
        ).fetchall()
        by_id = {row["id"]: dict(row) for row in job_rows}
        has_locations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='locations'"
        ).fetchone()
        locations = {}
        if has_locations:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(locations)")}
            remote = "is_remote" if "is_remote" in columns else "0 AS is_remote"
            hybrid = "is_hybrid" if "is_hybrid" in columns else "0 AS is_hybrid"
            for row in conn.execute(
                f"""SELECT DISTINCT job_posting_id, city_name, state_abbv, country, {remote}, {hybrid}
                    FROM locations WHERE job_posting_id IN ({','.join('?' for _ in selected_ids)})
                    ORDER BY country, state_abbv, city_name""", selected_ids,
            ):
                locations.setdefault(row["job_posting_id"], []).append(
                    {field: row[field] for field in ("city_name", "state_abbv", "country", "is_remote", "is_hybrid")}
                )
        jobs = []
        for position in chosen:
            job = by_id[int(ids[position])]
            index = title_indexes[position]
            job["score"] = float(row_scores[position]) if index >= 0 else None
            job["title_score"] = float(np.clip(title_scores[index], -1, 1)) if index >= 0 else None
            job["cluster_score"] = float(cluster[index]) if index >= 0 else None
            job["cluster_distance"] = float(distances[index]) if index >= 0 else None
            job["cluster_matched_title"] = profile["experiences"][int(cluster_best[index])]["title"] if index >= 0 else None
            job["position_estimated"] = bool(estimated[index]) if index >= 0 else None
            job["matched_title"] = titles[int(best[index])] if index >= 0 else None
            job["locations"] = locations.get(job["id"], [])
            jobs.append(job)
    return {"total": total, "page": page, "pages": pages, "jobs": jobs}


class Handler(BaseHTTPRequestHandler):
    def resume_local_host(self):
        host = self.headers.get("Host", "")
        try:
            return urlparse(f"http://{host}").hostname in ("127.0.0.1", "localhost")
        except ValueError:
            return False

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store" if urlparse(self.path).path.startswith("/api/resume-") else "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send_bytes(200, (WEBSITE_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/analyst":
            self.send_bytes(200, (WEBSITE_DIR / "analyst.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/matrix":
            self.send_bytes(200, (WEBSITE_DIR / "matrix.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/profile-creation":
            self.send_bytes(200, (WEBSITE_DIR / "resume_matcher.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/resume-matcher":
            self.send_bytes(200, (WEBSITE_DIR / "match_jobs.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/static/match_jobs.js":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "match_jobs.js").read_bytes(), "text/javascript")
        elif url.path == "/static/location_picker.js":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "location_picker.js").read_bytes(), "text/javascript")
        elif url.path == "/static/resume_matcher.js":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "resume_matcher.js").read_bytes(), "text/javascript")
        elif url.path == "/static/resume_matcher.css":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "resume_matcher.css").read_bytes(), "text/css")
        elif url.path == "/api/resume-profiles":
            try:
                if not self.resume_local_host():
                    raise ValueError("Candidate profiles are available only on localhost.")
                with _resume_lock:
                    body = json.dumps({"profiles": load_resume_profiles()}).encode()
                self.send_bytes(200, body, "application/json")
            except ValueError as error:
                self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except OSError as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path in ("/api/resume-matches", "/api/resume-index-status"):
            try:
                if not self.resume_local_host():
                    raise ValueError("Resume matching is available only on localhost.")
                data = (match_resume_jobs(parse_qs(url.query)) if url.path == "/api/resume-matches"
                        else title_index_status())
                self.send_bytes(200, json.dumps(data).encode(), "application/json")
            except ValueError as error:
                self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except TitleIndexUnavailable as error:
                self.send_bytes(409, json.dumps({"error": str(error)}).encode(), "application/json")
            except (OSError, sqlite3.Error) as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path == "/static/matrix.js":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "matrix.js").read_bytes(), "text/javascript")
        elif url.path == "/static/matrix.css":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "matrix.css").read_bytes(), "text/css")
        elif url.path.startswith("/api/matrix/"):
            try:
                body = json.dumps(matrix_api(url.path, parse_qs(url.query)), separators=(",", ":")).encode()
                self.send_bytes(200, body, "application/json")
            except ValueError as error:
                self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except MatrixStale as error:
                self.send_bytes(409, json.dumps({"error": str(error), "stale": True}).encode(), "application/json")
            except FileNotFoundError as error:
                self.send_bytes(503, json.dumps({"error": str(error)}).encode(), "application/json")
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
        elif url.path == "/api/top-cities":
            try:
                self.send_bytes(200, json.dumps(top_cities()).encode(), "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path == "/api/top-us-states":
            try:
                self.send_bytes(200, json.dumps(top_us_states()).encode(), "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path == "/api/top-countries":
            try:
                self.send_bytes(200, json.dumps(top_countries()).encode(), "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path == "/api/location-options":
            try:
                self.send_bytes(200, json.dumps(location_options()).encode(), "application/json")
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
            except ValueError as error:
                self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path == "/bookmarks":
            self.send_bytes(200, (WEBSITE_DIR / "bookmarks.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/static/bookmarks.js":
            self.send_bytes(200, (WEBSITE_DIR / "static" / "bookmarks.js").read_bytes(), "text/javascript")
        elif url.path == "/api/bookmarks":
            try:
                params = parse_qs(url.query)
                session = params.get("session", [""])[0]
                if not session:
                    raise ValueError("session parameter required")
                try:
                    page = max(1, int(params.get("page", ["1"])[0]))
                except ValueError:
                    page = 1
                body = json.dumps(get_bookmarks(session, page)).encode()
                self.send_bytes(200, body, "application/json")
            except ValueError as error:
                self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path == "/api/bookmark-add":
            self._handle_bookmark_request("add")
        elif url.path == "/api/bookmark-remove":
            self._handle_bookmark_request("remove")
        elif url.path == "/api/bookmark-check":
            try:
                params = parse_qs(url.query)
                session = params.get("session", [""])[0]
                job_id = params.get("job_id", [""])[0]
                if not session or not job_id:
                    raise ValueError("session and job_id parameters required")
                job_id = int(job_id)
                body = json.dumps(is_bookmarked(session, job_id)).encode()
                self.send_bytes(200, body, "application/json")
            except (ValueError, IndexError) as error:
                self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        else:
            self.send_bytes(404, b"Not found", "text/plain")
    
    def _handle_bookmark_request(self, action):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
            session = data.get("session_id")
            job_id = data.get("job_id")
            if not session or not job_id:
                raise ValueError("session_id and job_id required")
            job_id = int(job_id)
            if action == "add":
                result = add_bookmark(session, job_id)
            else:
                result = remove_bookmark(session, job_id)
            self.send_bytes(200, json.dumps(result).encode(), "application/json")
        except (ValueError, KeyError) as error:
            self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
        except sqlite3.Error as error:
            self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
    
    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/resume-index-build":
            try:
                if not self.resume_local_host():
                    raise ValueError("Resume matching is available only on localhost.")
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{self.headers.get('Host')}":
                    raise ValueError("Requests must originate from this website.")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Content-Type must be application/json.")
                self.send_bytes(200, json.dumps(start_title_index()).encode(), "application/json")
            except ValueError as error:
                self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except OSError as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path == "/api/resume-profiles":
            try:
                if not self.resume_local_host():
                    raise ValueError("Candidate profiles are available only on localhost.")
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{self.headers.get('Host')}":
                    raise ValueError("Requests must originate from this website.")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Content-Type must be application/json.")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("Request body must be between 1 byte and 1 MB.")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                body = json.dumps(change_resume_profile(data)).encode()
                self.send_bytes(200, body, "application/json")
            except (ValueError, UnicodeDecodeError) as error:
                self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except ResumeConflict as error:
                self.send_bytes(409, json.dumps({"error": str(error)}).encode(), "application/json")
            except OSError as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        elif url.path == "/api/bookmark-add":
            self._handle_bookmark_request("add")
        elif url.path == "/api/bookmark-remove":
            self._handle_bookmark_request("remove")
        elif url.path == "/api/bookmarks-clear":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode()
                data = json.loads(body)
                session = data.get("session_id")
                if not session:
                    raise ValueError("session_id required")
                with sqlite3.connect(DATABASE_PATH) as conn:
                    conn.execute("DELETE FROM bookmarks WHERE session_id = ?", (session,))
                    conn.commit()
                self.send_bytes(200, json.dumps({"success": True, "message": "All bookmarks cleared"}).encode(), "application/json")
            except (ValueError, KeyError) as error:
                self.send_bytes(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except sqlite3.Error as error:
                self.send_bytes(500, json.dumps({"error": str(error)}).encode(), "application/json")
        else:
            self.send_bytes(404, b"Not found", "text/plain")


if __name__ == "__main__":
    print("Serving on http://127.0.0.1:5000/")
    ThreadingHTTPServer(("127.0.0.1", 5000), Handler).serve_forever()
