"""Build a local semantic index of every distinct job title."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT.parent / "The_Database" / "job_data.db"
INDEX = ROOT / "data" / "title_embeddings.sqlite3"
MODEL = "sentence-transformers/all-MiniLM-L6-v2"
REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
DIMENSIONS = 384
MODEL_KEY = f"{MODEL}@{REVISION}"


def load_model():
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(2)
    return SentenceTransformer(MODEL, revision=REVISION, local_files_only=True, device="cpu")


def encode(model, titles):
    vectors = model.encode(titles, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.shape != (len(titles), DIMENSIONS) or not np.isfinite(vectors).all():
        raise ValueError("The embedding model produced invalid vectors.")
    return vectors


def build(database=DATABASE, index=INDEX):
    if not database.is_file():
        raise FileNotFoundError(f"Job database not found: {database}")
    index.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as jobs:
        titles = [row[0] for row in jobs.execute(
            "SELECT DISTINCT job_title FROM job_postings WHERE job_title IS NOT NULL AND trim(job_title) != ''"
        )]
    with sqlite3.connect(index) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS titles (title TEXT PRIMARY KEY, embedding BLOB NOT NULL)")
        metadata = dict(conn.execute("SELECT key, value FROM metadata"))
        if metadata and metadata != {"model": MODEL_KEY, "dimensions": str(DIMENSIONS)}:
            raise ValueError("The title index uses a different model or format.")
        if not metadata:
            if conn.execute("SELECT 1 FROM titles LIMIT 1").fetchone():
                raise ValueError("The title index has data but no model metadata.")
            conn.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("model", MODEL_KEY), ("dimensions", str(DIMENSIONS)),
            ])
            conn.commit()
        existing = {row[0] for row in conn.execute("SELECT title FROM titles")}
        missing = [title for title in titles if title not in existing]
        print(f"Indexing {len(missing):,} missing titles out of {len(titles):,} total.", flush=True)
        if not missing:
            print("Title index is up to date.", flush=True)
            return
        model = load_model()
        for offset in range(0, len(missing), 1024):
            batch = missing[offset:offset + 1024]
            vectors = encode(model, batch)
            conn.executemany(
                "INSERT OR IGNORE INTO titles VALUES (?, ?)",
                ((title, vector.astype("<f2").tobytes()) for title, vector in zip(batch, vectors)),
            )
            conn.commit()
            print(f"Indexed {min(offset + len(batch), len(missing)):,} / {len(missing):,} new titles.", flush=True)
    print("Title index is up to date.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "encode"])
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if args.command == "build":
        build()
    else:
        titles = json.load(sys.stdin)
        if not isinstance(titles, list) or not titles or not all(isinstance(t, str) and t.strip() for t in titles):
            raise ValueError("A nonempty list of job titles is required.")
        print(json.dumps(encode(load_model(), titles).tolist()))


if __name__ == "__main__":
    main()
