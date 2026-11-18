"""Place titles and experience descriptions relative to the existing Job Matrix."""

import argparse
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "Job Matrix"))
import job_matrix
import title_index


def interpolate_positions(queries, reference_vectors, reference_positions, k=8):
    queries = np.asarray(queries, dtype=np.float32)
    references = np.asarray(reference_vectors, dtype=np.float32)
    positions = np.asarray(reference_positions, dtype=np.float32)
    if not len(references) or len(references) != len(positions):
        raise ValueError("The Job Matrix has no valid reference points.")
    if queries.ndim != 2 or references.ndim != 2 or queries.shape[1] != references.shape[1]:
        raise ValueError("Embedding dimensions do not match the Job Matrix.")
    norms = np.linalg.norm(references, axis=1)
    query_norms = np.linalg.norm(queries, axis=1)
    if not np.isfinite(norms).all() or not np.isfinite(query_norms).all() or np.any(norms == 0) or np.any(query_norms == 0):
        raise ValueError("Cannot place an invalid embedding on the Job Matrix.")
    references = references / norms[:, None]
    queries = queries / query_norms[:, None]
    k = min(k, len(references))
    result = np.empty((len(queries), 3), dtype=np.float32)
    for start in range(0, len(queries), 512):
        similarities = queries[start:start + 512] @ references.T
        top = np.argpartition(-similarities, k - 1, axis=1)[:, :k]
        weights = np.maximum(np.take_along_axis(similarities, top, axis=1), 1e-3) ** 4
        result[start:start + len(top)] = (
            (positions[top] * weights[:, :, None]).sum(axis=1) / weights.sum(axis=1)[:, None]
        )
    return result


def title_positions(index_path, matrix_folder):
    metadata = json.loads((matrix_folder / "meta.json").read_text(encoding="utf-8"))
    nodes = metadata["nodes"]
    with np.load(matrix_folder / "matrix.npz") as matrix:
        positions = matrix["pos"].astype(np.float32)
    if positions.shape != (len(nodes), 3) or not np.isfinite(positions).all():
        raise ValueError("The Job Matrix contains invalid positions.")
    if not nodes:
        raise ValueError("The Job Matrix has no title points.")
    node_keys = [node["key"] for node in nodes]
    node_lookup = {key: i for i, key in enumerate(node_keys)}
    node_vectors = title_index.encode(title_index.load_model(), node_keys)
    with sqlite3.connect(f"file:{index_path}?mode=ro", uri=True) as conn:
        conn.execute("BEGIN")
        count = conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
        result = np.empty((count, 3), dtype=np.float32)
        estimated = np.empty(count, dtype=bool)
        cursor = conn.execute("SELECT title, embedding FROM titles ORDER BY rowid")
        cache = {}
        offset = 0
        while rows := cursor.fetchmany(512):
            unknown_vectors = []
            unknown_indexes = []
            for i, (title, raw) in enumerate(rows):
                node = node_lookup.get(job_matrix.normalise_title(title, cache))
                if node is not None:
                    result[offset + i] = positions[node]
                    estimated[offset + i] = False
                else:
                    vector = np.frombuffer(raw, dtype="<f2")
                    if vector.shape != (title_index.DIMENSIONS,):
                        raise ValueError("The title index contains an invalid vector.")
                    unknown_vectors.append(vector)
                    unknown_indexes.append(offset + i)
            if unknown_indexes:
                result[unknown_indexes] = interpolate_positions(
                    np.asarray(unknown_vectors), node_vectors, positions,
                )
                estimated[unknown_indexes] = True
            offset += len(rows)
    return result, estimated


def experience_embeddings(descriptions):
    import torch
    from sentence_transformers import SentenceTransformer

    if not isinstance(descriptions, list) or not descriptions or not all(
        isinstance(text, str) and text.strip() for text in descriptions
    ):
        raise ValueError("Each unplaced experience needs a description.")
    torch.set_num_threads(2)
    model = SentenceTransformer(job_matrix.MODEL_NAME, local_files_only=True, device="cpu")
    vectors = np.asarray(
        model.encode(descriptions, batch_size=4, normalize_embeddings=True, show_progress_bar=False),
        dtype=np.float32,
    )
    if vectors.ndim != 2 or len(vectors) != len(descriptions) or not np.isfinite(vectors).all():
        raise ValueError("The Job Matrix model returned invalid embeddings.")
    return vectors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["title-positions", "experience-embeddings"])
    parser.add_argument("--index", type=Path)
    parser.add_argument("--matrix", type=Path)
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if args.command == "title-positions":
        if args.index is None or args.matrix is None:
            parser.error("--index and --matrix are required")
        positions, estimated = title_positions(args.index, args.matrix)
        output = io.BytesIO()
        np.save(output, positions, allow_pickle=False)
        np.save(output, estimated, allow_pickle=False)
    else:
        output = io.BytesIO()
        np.save(output, experience_embeddings(json.load(sys.stdin)), allow_pickle=False)
    sys.stdout.buffer.write(output.getvalue())


if __name__ == "__main__":
    main()
