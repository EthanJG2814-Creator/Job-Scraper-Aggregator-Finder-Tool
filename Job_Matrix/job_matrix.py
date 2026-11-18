"""Job Matrix builder: keeps the matrix in sync with the job database.

    python job_matrix.py update             # refresh from the database (incremental)
    python job_matrix.py update --full      # also recompute the layout and families from scratch
    python job_matrix.py update --max-embed 2000
    python job_matrix.py watch --every 30   # run `update` whenever new located postings appear

Only postings that have a country count; postings without a location are ignored everywhere.
A "job" in the matrix is a normalised job title with at least MIN_POSTINGS located postings (the
most widely used MAX_NODES titles when there are more). Each job is described by one real
description (never truncated), embedded with Qwen3-Embedding, and linked to similar jobs.

Embeddings, layout positions and families are remembered per title in data/registry.npz, so an
update only embeds titles it has not seen (or that have grown a lot) and existing jobs do not
move. Results are published to data/v<timestamp>/ and data/current.txt is switched last, so the
website never reads half-written files.

The builder needs pandas/pyarrow (one-time import of the old matrix), scikit-learn, umap-learn and
sentence-transformers. The website itself only needs numpy + scipy.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DB_PATH = Path(os.environ.get("JOB_MATRIX_DB", HERE.parent / "The_Database" / "job_data.db"))
REGISTRY_PATH = DATA_DIR / "registry.npz"
POINTER_PATH = DATA_DIR / "current.txt"
SIGNATURE_PATH = DATA_DIR / "last_signature.txt"
LOCK_PATH = DATA_DIR / ".update.lock"


def _env_int(name, default):
    return int(os.environ.get(name, default))


MIN_POSTINGS = _env_int("JOB_MATRIX_MIN_POSTINGS", 2)    # located postings a title needs to become a job
MAX_NODES = _env_int("JOB_MATRIX_MAX_NODES", 25_000)     # keep the most widely used titles beyond this
TRUSTED_MIN_COMPANIES = 3    # titles used by this many companies form the routing network
N_FAMILIES = 30
K_GRAPH = 10                 # nearest trusted neighbours each trusted node links to
K_ANCHORS = 5                # trusted titles each one-off title is attached to

MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
SAMPLE_POSTINGS = 200        # postings of the top company used to find the sentences a title shares
MIN_SHARE = 0.5              # a sentence is kept if it appears in this share of those descriptions
EMBED_CHUNK = 250
REEMBED_GROWTH, REEMBED_MIN = 3.0, 6    # re-embed a title once it has this many times more postings
KEEP_VERSIONS = 2


# ---------------------------------------------------------------------------------------------
# Titles and descriptions (same normalisation the embedding notebook used)
# ---------------------------------------------------------------------------------------------
def title_key(t):
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def coarse_key(t):
    t = re.sub(r"#\s*\d+", " ", t)
    t = re.sub(r"\b(level|tier|grade)\s+\d+\b", " ", t)
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"\s+(\d+|i{1,3}|iv|v)$", "", t.strip())
    return re.sub(r"\s+", " ", t).strip() or t


def normalise_title(raw, cache):
    key = cache.get(raw)
    if key is None:
        tk = title_key(raw)
        key = cache[raw] = coarse_key(tk) if tk else ""
    return key


def clean_label(title):
    """Readable display name from the punctuation-stripped title key."""
    label = re.sub(r"\b(\w+) s\b", r"\1's", title)
    label = re.sub(r"\bamp\b", "&", label)
    return label.title().replace("'S", "'s")


def split_units(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+|•", text)
    return [p.strip() for p in parts if p.strip()]


def ukey(u):
    return hashlib.md5(re.sub(r"[^a-z0-9 ]", "", u.lower()).encode()).hexdigest()


def core_description(descs):
    """Sentences shared by at least MIN_SHARE of the descriptions (drops one-off text)."""
    descs = [d for d in descs if d]
    if not descs:
        return ""
    if len(descs) == 1:
        return descs[0]
    seen, first_text = Counter(), {}
    for d in descs:
        keys = set()
        for u in split_units(d):
            k = ukey(u)
            keys.add(k)
            first_text.setdefault(k, u)
        seen.update(keys)
    need = MIN_SHARE * len(descs)
    kept = [first_text[k] for k in first_text if seen[k] >= need]
    return " ".join(kept) if kept else descs[0]


# ---------------------------------------------------------------------------------------------
# Reading the database (only postings that have a country)
# ---------------------------------------------------------------------------------------------
def connect():
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def located_postings(conn):
    """(posting id, company id, raw title, 'US,CA') for every posting with at least one country."""
    return conn.execute("""
        SELECT j.job_posting_id, j.company_id, j.job_title, group_concat(DISTINCT l.country)
        FROM job_postings j JOIN locations l ON l.job_posting_id = j.job_posting_id
        WHERE j.job_title IS NOT NULL AND l.country IS NOT NULL AND l.country <> ''
        GROUP BY j.job_posting_id""")


def db_signature(conn):
    """Cheap fingerprint that changes when postings or locations are added or removed."""
    postings = conn.execute("SELECT COUNT(*), COALESCE(MAX(job_posting_id), 0) FROM job_postings").fetchone()
    locations = conn.execute("SELECT COUNT(*), COALESCE(MAX(location_id), 0) FROM locations").fetchone()
    return ":".join(str(v) for v in (*postings, *locations))


def scan_titles(conn):
    """Pass 1: located postings and the companies behind each normalised title."""
    cache, stats = {}, {}
    for _, company, raw, _ in located_postings(conn):
        key = normalise_title(raw, cache)
        if not key:
            continue
        entry = stats.get(key)
        if entry is None:
            entry = stats[key] = [0, Counter()]
        entry[0] += 1
        entry[1][company] += 1
    return stats


def select_nodes(stats, min_postings=MIN_POSTINGS, max_nodes=MAX_NODES):
    """Titles that become jobs, most widely used first."""
    eligible = [key for key, (n, _) in stats.items() if n >= min_postings]
    eligible.sort(key=lambda key: (-len(stats[key][1]), -stats[key][0], key))
    return eligible[:max_nodes]


def collect_details(conn, keys, stats, sample_for):
    """Pass 2: per-country [postings, companies] for each job, plus sample posting ids.

    sample_for: titles that need a description; up to SAMPLE_POSTINGS postings of their
    most active company are returned for them.
    """
    wanted = set(keys)
    top_company = {key: stats[key][1].most_common(1)[0][0] for key in sample_for}
    countries = {key: {} for key in keys}
    samples = {key: [] for key in sample_for}
    cache = {}
    for pid, company, raw, codes in located_postings(conn):
        key = normalise_title(raw, cache)
        if key not in wanted:
            continue
        for code in set(codes.upper().split(",")):
            entry = countries[key].setdefault(code, [0, set()])
            entry[0] += 1
            entry[1].add(company)
        if key in samples and company == top_company[key] and len(samples[key]) < SAMPLE_POSTINGS:
            samples[key].append(pid)
    counts = {key: {code: [n, len(c)] for code, (n, c) in per.items()} for key, per in countries.items()}
    return counts, samples


def fetch_descriptions(conn, ids, chunk=500):
    out = {}
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        query = ("SELECT job_posting_id, job_description FROM job_postings "
                 f"WHERE job_posting_id IN ({','.join('?' * len(part))})")
        out.update(conn.execute(query, part))
    return out


# ---------------------------------------------------------------------------------------------
# Registry: what we remember about every title between updates
# ---------------------------------------------------------------------------------------------
class Registry:
    """title -> embedding, postings at embed time, snippet, 3D position, family."""

    def __init__(self):
        self.rows = {}
        self.centroids = None

    def has_embedding(self, key):
        return key in self.rows

    def needs_embedding(self, key, postings):
        row = self.rows.get(key)
        if row is None:
            return True
        return postings >= REEMBED_MIN and postings >= REEMBED_GROWTH * row["n"]

    def set_embedding(self, key, vector, postings, snippet):
        row = self.rows.get(key)
        if row is None:
            row = self.rows[key] = {"pos": None, "family": -1}
        row.update(emb=np.asarray(vector, dtype=np.float16), n=int(postings), snippet=snippet)

    @classmethod
    def load(cls):
        reg = cls()
        if not REGISTRY_PATH.exists():
            return reg
        with np.load(REGISTRY_PATH) as data:
            for key, emb, n, snippet, pos, family in zip(
                    data["titles"], data["emb"], data["n"], data["snippet"], data["pos"], data["family"]):
                reg.rows[str(key)] = {"emb": emb, "n": int(n), "snippet": str(snippet),
                                      "pos": pos if np.isfinite(pos[0]) else None, "family": int(family)}
            if data["centroids"].size:
                reg.centroids = data["centroids"]
        return reg

    def save(self):
        keys = list(self.rows)
        dim = self.rows[keys[0]]["emb"].shape[0] if keys else 1024
        arrays = dict(
            titles=np.array(keys, dtype=str),
            emb=np.array([self.rows[k]["emb"] for k in keys], dtype=np.float16).reshape(len(keys), dim),
            n=np.array([self.rows[k]["n"] for k in keys], dtype=np.int32),
            snippet=np.array([self.rows[k]["snippet"] for k in keys], dtype=str),
            pos=np.array([self.rows[k]["pos"] if self.rows[k]["pos"] is not None else [np.nan] * 3
                          for k in keys], dtype=np.float32).reshape(len(keys), 3),
            family=np.array([self.rows[k]["family"] for k in keys], dtype=np.int16),
            centroids=self.centroids if self.centroids is not None else np.zeros((0, dim), dtype=np.float32),
        )
        DATA_DIR.mkdir(exist_ok=True)
        tmp = REGISTRY_PATH.with_name("registry.tmp.npz")
        np.savez(tmp, **arrays)
        os.replace(tmp, REGISTRY_PATH)


def import_legacy_matrix(reg):
    """One-time import of the first (notebook-built) matrix so its embeddings are not recomputed."""
    nodes_path, emb_path, matrix_path = (DATA_DIR / name for name in ("nodes.parquet", "embeddings.npy", "matrix.npz"))
    if not (nodes_path.exists() and emb_path.exists() and matrix_path.exists()):
        return
    import pandas as pd

    nodes = pd.read_parquet(nodes_path, columns=["title", "n_postings", "snippet"])
    emb = np.load(emb_path, mmap_mode="r")
    with np.load(matrix_path) as arrays:
        pos, family = arrays["pos"], arrays["family"]
    for i, (key, n, snippet) in enumerate(zip(nodes["title"], nodes["n_postings"], nodes["snippet"])):
        reg.set_embedding(key, emb[i], n, snippet)
        reg.rows[key]["pos"] = pos[i].astype(np.float32)
        reg.rows[key]["family"] = int(family[i])
    dense = np.asarray(emb, dtype=np.float32)
    centroids = np.stack([dense[family == f].mean(axis=0) if (family == f).any() else np.zeros(dense.shape[1])
                          for f in range(int(family.max()) + 1)])
    reg.centroids = unit_rows(centroids)
    print(f"imported {len(nodes):,} jobs from the first matrix build")


def unit_rows(matrix):
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)


# ---------------------------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------------------------
def load_model():
    try:
        import torch
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("sentence-transformers is not installed here; new titles cannot be embedded in this run.")
        return None
    torch.set_num_threads(_env_int("JOB_MATRIX_THREADS", min(8, os.cpu_count() or 4)))
    return SentenceTransformer(MODEL_NAME)


def embed_titles(conn, reg, need, samples, stats):
    model = load_model()
    if model is None:
        return 0
    done, t0, last_save = 0, time.time(), 0
    for s in range(0, len(need), EMBED_CHUNK):
        batch = need[s:s + EMBED_CHUNK]
        descriptions = fetch_descriptions(conn, [pid for key in batch for pid in samples[key]])
        keys, texts = [], []
        for key in batch:
            text = core_description([descriptions.get(pid) for pid in samples[key]])   # embedded in full
            if text.strip():
                keys.append(key)
                texts.append(text)
        if texts:
            vectors = model.encode(texts, batch_size=4, normalize_embeddings=True, show_progress_bar=False)
            for key, text, vector in zip(keys, texts, vectors):
                reg.set_embedding(key, vector, stats[key][0], text[:500])
        done += len(batch)
        if done - last_save >= 1000 or done == len(need):
            reg.save()
            last_save = done
        print(f"embedded {done:,}/{len(need):,} ({done / max(time.time() - t0, 1e-9):.1f}/sec)", flush=True)
    return done


# ---------------------------------------------------------------------------------------------
# Graph, families, layout
# ---------------------------------------------------------------------------------------------
STOPWORDS = {"and", "the", "of", "for", "in", "at", "to", "a", "an", "with", "amp", "i", "ii", "iii",
             "full", "part", "time", "ft", "pt", "prn", "new", "us", "h", "f", "m", "w", "d"}


def family_labels(titles, weights, family, n_families):
    """Name each family by its most distinctive title words (class-based tf-idf)."""
    per_family = [Counter() for _ in range(n_families)]
    for title, w, fam in zip(titles, weights, family):
        for word in set(title.split()):
            if len(word) > 2 and word not in STOPWORDS and word.isalpha():
                per_family[fam][word] += np.log1p(w)
    totals = Counter()
    for counter in per_family:
        totals.update(counter)
    avg = sum(totals.values()) / n_families
    labels = []
    for counter in per_family:
        scored = sorted(counter, key=lambda word: counter[word] * np.log(1 + avg / totals[word]), reverse=True)
        labels.append(" / ".join(scored[:3]).title())
    return labels


def knn(emb, k, chunk=2000):
    """Exact cosine kNN (rows are unit length). Returns (ids, sims) excluding self."""
    ids = np.empty((len(emb), k), dtype=np.int32)
    sims = np.empty((len(emb), k), dtype=np.float32)
    for s in range(0, len(emb), chunk):
        block = emb[s:s + chunk] @ emb.T
        block[np.arange(len(block)), np.arange(s, s + len(block))] = -np.inf
        top = np.argpartition(-block, k, axis=1)[:, :k]
        top_sims = np.take_along_axis(block, top, axis=1)
        order = np.argsort(-top_sims, axis=1)
        ids[s:s + chunk] = np.take_along_axis(top, order, axis=1)
        sims[s:s + chunk] = np.take_along_axis(top_sims, order, axis=1)
    return ids, sims


def sym_graph(rows, cols, sims, n):
    from scipy.sparse import coo_matrix
    graph = coo_matrix(((1.0 - sims).clip(min=1e-4), (rows, cols)), shape=(n, n)).tocsr()
    return graph.maximum(graph.T).tocsr()   # edges in either direction; weight is cosine distance


def build_graph(emb, trusted, k=K_GRAPH, anchors=K_ANCHORS):
    """Routing graph.

    Trusted titles (used by several companies) are linked to their k nearest trusted titles.
    One-off titles are attached only to their nearest trusted titles, so they can be searched and
    used as endpoints but routes do not hop through them.
    """
    from scipy.sparse.csgraph import connected_components

    n = len(emb)
    k = min(k, max(int(trusted.sum()) - 1, 1))
    anchors = min(anchors, int(trusted.sum()))
    t_idx = np.where(trusted)[0]
    u_idx = np.where(~trusted)[0]
    t_emb = emb[t_idx]

    ids, sims = knn(t_emb, k)
    rows = [t_idx[np.repeat(np.arange(len(t_idx)), k)]]
    cols = [t_idx[ids.ravel()]]
    vals = [sims.ravel()]

    for s in range(0, len(u_idx), 2000):
        part = u_idx[s:s + 2000]
        block = emb[part] @ t_emb.T
        top = np.argpartition(-block, anchors, axis=1)[:, :anchors] if anchors < block.shape[1] \
            else np.tile(np.arange(block.shape[1]), (len(part), 1))
        rows.append(np.repeat(part, top.shape[1]))
        cols.append(t_idx[top.ravel()])
        vals.append(np.take_along_axis(block, top, axis=1).ravel())

    graph = sym_graph(np.concatenate(rows), np.concatenate(cols), np.concatenate(vals), n)

    n_comp, labels = connected_components(graph, directed=False)
    if n_comp > 1:
        main = np.bincount(labels).argmax()
        main_idx = np.where((labels == main) & trusted)[0]
        extra = []
        for comp in set(labels) - {main}:
            members = np.where((labels == comp) & trusted)[0]
            if not len(members):
                members = np.where(labels == comp)[0]
            sim = emb[members] @ emb[main_idx].T
            i, j = np.unravel_index(sim.argmax(), sim.shape)
            extra.append((members[i], main_idx[j], sim[i, j]))
        r, c, v = zip(*extra)
        graph = graph.maximum(sym_graph(np.array(r), np.array(c), np.array(v), n)).tocsr()
    return graph


def layout_3d(emb):
    """Full 3D layout (slow); used for the first build and for --full."""
    n = len(emb)
    if n < 60:
        from sklearn.decomposition import PCA
        pos = PCA(n_components=3, random_state=0).fit_transform(emb).astype("float32")
    else:
        import umap
        pos = umap.UMAP(n_components=3, n_neighbors=15, min_dist=0.35, metric="cosine",
                        random_state=42).fit_transform(emb).astype("float32")
    pos -= pos.mean(axis=0)
    return pos / max(float(np.abs(pos).max()), 1e-6) * 100


def place_new(emb, pos, k=8, seed=0):
    """Fill NaN rows of pos: the similarity-weighted mean of the nearest placed jobs, plus a little jitter."""
    placed = np.where(np.isfinite(pos[:, 0]))[0]
    fresh = np.where(~np.isfinite(pos[:, 0]))[0]
    rng = np.random.default_rng(seed)
    k = min(k, len(placed))
    for s in range(0, len(fresh), 2000):
        part = fresh[s:s + 2000]
        sims = emb[part] @ emb[placed].T
        top = np.argpartition(-sims, k - 1, axis=1)[:, :k] if k < sims.shape[1] \
            else np.tile(np.arange(sims.shape[1]), (len(part), 1))
        weight = np.clip(np.take_along_axis(sims, top, axis=1), 1e-3, None) ** 4
        near = pos[placed][top]                                   # (m, k, 3)
        mean = (weight[..., None] * near).sum(axis=1) / weight.sum(axis=1)[:, None]
        spread = near.std(axis=1).mean(axis=1, keepdims=True)
        pos[part] = mean + rng.normal(size=mean.shape) * spread * 0.5
    return pos


def assign_families(emb, reg_family, centroids):
    """Keep known families; send new jobs to the nearest family centroid."""
    family = reg_family.copy()
    unset = family < 0
    if unset.any():
        family[unset] = (emb[unset] @ centroids.T).argmax(axis=1)
    return family


def cluster_families(emb):
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA

    n_families = min(N_FAMILIES, len(emb))
    reduced = PCA(n_components=min(50, len(emb) - 1), random_state=0).fit_transform(emb)
    family = KMeans(n_clusters=n_families, n_init="auto", random_state=42).fit_predict(reduced)
    centroids = unit_rows(np.stack([emb[family == f].mean(axis=0) for f in range(n_families)]))
    return family.astype(int), centroids


# ---------------------------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------------------------
def publish(keys, stats, counts, emb, pos, family, centroid_count, reg):
    trusted = np.array([len(stats[k][1]) >= TRUSTED_MIN_COMPANIES for k in keys])
    graph = build_graph(emb, trusted)
    postings = [stats[k][0] for k in keys]
    labels = family_labels(keys, postings, family, max(centroid_count, int(family.max()) + 1))

    version = f"v{int(time.time())}"
    out = DATA_DIR / version
    out.mkdir(parents=True)
    np.savez_compressed(out / "matrix.npz", pos=pos, family=family.astype("int16"), trusted=trusted,
                        indptr=graph.indptr, indices=graph.indices, weights=graph.data.astype("float32"))
    np.save(out / "embeddings.npy", emb.astype(np.float16))
    nodes = [{"id": i, "title": clean_label(k), "key": k, "postings": int(stats[k][0]),
              "companies": len(stats[k][1]), "snippet": reg.rows[k]["snippet"]} for i, k in enumerate(keys)]
    (out / "meta.json").write_text(json.dumps({"version": version, "families": labels, "nodes": nodes},
                                              separators=(",", ":")), encoding="utf-8")
    (out / "countries.json").write_text(json.dumps({"nodes": [counts[k] for k in keys]},
                                                   separators=(",", ":")), encoding="utf-8")
    tmp = POINTER_PATH.with_name("current.tmp")
    tmp.write_text(version, encoding="utf-8")
    os.replace(tmp, POINTER_PATH)   # the switch: readers see either the old version or the new one

    old = sorted((p for p in DATA_DIR.glob("v[0-9]*") if p.is_dir()), key=lambda p: p.name)
    for stale in old[:-KEEP_VERSIONS]:
        shutil.rmtree(stale, ignore_errors=True)
    return version, graph.nnz // 2


# ---------------------------------------------------------------------------------------------
# The update
# ---------------------------------------------------------------------------------------------
def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextmanager
def update_lock():
    DATA_DIR.mkdir(exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                pid = int(LOCK_PATH.read_text() or 0)
            except (ValueError, OSError):
                pid = 0
            if pid and pid_alive(pid):
                raise SystemExit(f"Another Job Matrix update is already running (pid {pid}).")
            LOCK_PATH.unlink(missing_ok=True)
    else:
        raise SystemExit("Could not take the Job Matrix update lock.")
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def update(full=False, max_embed=None, if_changed=False):
    with update_lock():
        t0 = time.time()
        conn = connect()
        signature = db_signature(conn)
        if if_changed and POINTER_PATH.exists() and SIGNATURE_PATH.exists() \
                and SIGNATURE_PATH.read_text() == signature:
            print("Job Matrix: no new located postings.")
            return False

        stats = scan_titles(conn)
        keys = select_nodes(stats)
        print(f"{sum(n for n, _ in stats.values()):,} located postings, {len(stats):,} titles, "
              f"{len(keys):,} become jobs ({time.time() - t0:.0f}s)")
        if not keys:
            raise SystemExit("No located postings to build a matrix from yet.")

        reg = Registry.load()
        if not reg.rows:
            import_legacy_matrix(reg)
        need = [k for k in keys if reg.needs_embedding(k, stats[k][0])]
        if max_embed:
            need = need[:max_embed]
        counts, samples = collect_details(conn, keys, stats, need)
        if need:
            print(f"embedding {len(need):,} titles", flush=True)
            embed_titles(conn, reg, need, samples, stats)

        keys = [k for k in keys if reg.has_embedding(k)]
        if len(keys) < 10:
            raise SystemExit("Too few embedded jobs to build a matrix.")
        emb = unit_rows(np.stack([reg.rows[k]["emb"] for k in keys]))
        pos = np.array([reg.rows[k]["pos"] if reg.rows[k]["pos"] is not None else [np.nan] * 3
                        for k in keys], dtype=np.float32)
        family = np.array([reg.rows[k]["family"] for k in keys], dtype=int)

        placed = np.isfinite(pos[:, 0]).mean()
        if full or reg.centroids is None or placed < 0.5:
            print("computing layout and families from scratch ...", flush=True)
            pos = layout_3d(emb)
            family, reg.centroids = cluster_families(emb)
        else:
            if (~np.isfinite(pos[:, 0])).any():
                pos = place_new(emb, pos)
            family = assign_families(emb, family, reg.centroids)
        for k, p, f in zip(keys, pos, family):
            reg.rows[k]["pos"], reg.rows[k]["family"] = p, int(f)
        reg.save()

        version, edges = publish(keys, stats, counts, emb, pos, family, len(reg.centroids), reg)
        SIGNATURE_PATH.write_text(signature)
        print(f"Job Matrix {version}: {len(keys):,} jobs, {edges:,} links ({time.time() - t0:.0f}s)")
        return True


def watch(every_minutes, **kwargs):
    print(f"Watching {DB_PATH.name} every {every_minutes} min (Ctrl+C to stop)", flush=True)
    try:
        while True:
            try:
                update(if_changed=True, **kwargs)
            except SystemExit as stop:
                print(stop, flush=True)
            except Exception as error:   # keep watching through transient failures
                print(f"Job Matrix update failed: {error}", flush=True)
            time.sleep(every_minutes * 60)
    except KeyboardInterrupt:
        print("Stopped.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("update", help="refresh the matrix from the database")
    up.add_argument("--full", action="store_true", help="recompute layout and families from scratch")
    up.add_argument("--max-embed", type=int, default=None, help="embed at most this many titles this run")
    up.add_argument("--if-changed", action="store_true", help="do nothing if no postings changed since the last update")
    wa = commands.add_parser("watch", help="keep running `update` when the database changes")
    wa.add_argument("--every", type=float, default=30, help="minutes between checks (default 30)")
    wa.add_argument("--max-embed", type=int, default=None)
    args = parser.parse_args()
    try:
        os.nice(10)   # stay out of the way of the scrapers
    except (AttributeError, OSError):
        pass
    if args.command == "update":
        update(full=args.full, max_embed=args.max_embed, if_changed=args.if_changed)
    else:
        watch(args.every, max_embed=args.max_embed)


if __name__ == "__main__":
    main()

#python "Job Matrix/job_matrix.py" update