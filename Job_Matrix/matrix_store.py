"""Runtime side of the Job Matrix: loads data/ and answers search and path queries.

Only needs numpy + scipy. The files in data/ are produced by job_matrix.py.

A country selection (a set of ISO codes) limits
search, the 3D view and routes to jobs seen in those countries. Routes are computed on a graph
rebuilt for that selection, so they never pass through jobs outside it.
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

DATA_DIR = Path(__file__).resolve().parent / "data"


def current_data_dir(root=DATA_DIR):
    """The folder job_matrix.py last published (data/current.txt), else the old flat layout."""
    pointer = root / "current.txt"
    if pointer.exists():
        folder = root / pointer.read_text(encoding="utf-8").strip()
        if (folder / "meta.json").exists():
            return folder
    return root

# Cost of a hop is (cosine distance) ** COST_EXPONENT. Above 1, many small steps beat a few big
# jumps, which is what separates the "closest" route from the "fewest jobs" route.
COST_EXPONENT = 2.0
HOP_TIE_BREAK = 1e-3   # on equal hop counts, prefer the shorter distance

MIN_ROUTING_NODES = 40    # small selections still get a usable routing network
GRAPH_CACHE_SIZE = 12


def country_name(code):
    try:
        import pycountry
        country = pycountry.countries.get(alpha_2=code)
        return getattr(country, "common_name", None) or country.name if country else code
    except ImportError:
        return code


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _grams(text):
    s = f"^{_squash(text)}$"
    return {s[i:i + 3] for i in range(len(s) - 2)}


class MatrixStore:
    def __init__(self, data_dir=None):
        data_dir = data_dir or current_data_dir()
        arrays = np.load(data_dir / "matrix.npz")
        meta = json.loads((data_dir / "meta.json").read_text(encoding="utf-8"))
        self.version = meta.get("version", "legacy")
        self.pos = arrays["pos"]
        self.family = arrays["family"].astype(int)
        self.families = meta["families"]
        self.nodes = meta["nodes"]
        n = len(self.nodes)
        self.n = n
        dist = csr_matrix((arrays["weights"], arrays["indices"], arrays["indptr"]), shape=(n, n))
        self.trusted = arrays["trusted"]
        self.graph = self._make_graph(dist.maximum(dist.T).tocsr())
        self._embeddings = np.load(data_dir / "embeddings.npy", mmap_mode="r")
        self._graphs = {}
        self._load_countries(data_dir / "countries.json")
        self._build_search_index()

    @staticmethod
    def _make_graph(dist):
        """dist holds the raw cosine distance per edge; cost and hops are what routes minimise."""
        cost = dist.copy()
        cost.data = cost.data ** COST_EXPONENT
        hops = dist.copy()
        hops.data = 1.0 + HOP_TIE_BREAK * dist.data
        return SimpleNamespace(dist=dist, cost=cost, hops=hops)

    # ---- countries ----------------------------------------------------------------------
    def _load_countries(self, path):
        self.country_stats = None
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if len(data["nodes"]) != self.n:
            return
        self.country_stats = data["nodes"]
        totals = {}
        for stats in self.country_stats:
            for code, (postings, _) in stats.items():
                row = totals.setdefault(code, [0, 0])
                row[0] += 1
                row[1] += postings
        self.country_totals = totals

    def country_list(self):
        if self.country_stats is None:
            return {"countries": []}
        rows = [{"code": code, "name": country_name(code), "jobs": jobs, "postings": postings}
                for code, (jobs, postings) in self.country_totals.items()]
        rows.sort(key=lambda r: -r["postings"])
        return {"countries": rows}

    def selection(self, codes):
        """Normalise a requested set of codes; None means no filtering."""
        if self.country_stats is None or not codes:
            return None
        chosen = frozenset(code for code in codes if code in self.country_totals)
        return None if chosen == frozenset(self.country_totals) else chosen

    def active_mask(self, chosen):
        if chosen is None:
            return np.ones(self.n, dtype=bool)
        return np.fromiter((not chosen.isdisjoint(stats) for stats in self.country_stats),
                           dtype=bool, count=self.n)

    def graph_for(self, chosen):
        if chosen is None:
            return self.graph
        if chosen in self._graphs:
            return self._graphs[chosen]
        from job_matrix import TRUSTED_MIN_COMPANIES, build_graph

        idx = np.where(self.active_mask(chosen))[0]
        n = self.n
        if len(idx) < 2:
            graph = self._make_graph(csr_matrix((n, n)))
        else:
            companies = np.array([sum(self.country_stats[i][c][1] for c in chosen if c in self.country_stats[i])
                                  for i in idx])
            trusted = companies >= TRUSTED_MIN_COMPANIES
            if trusted.sum() < min(MIN_ROUTING_NODES, len(idx)):
                trusted[np.argsort(-companies)[:min(MIN_ROUTING_NODES, len(idx))]] = True
            vectors = np.asarray(self._embeddings[idx], dtype=np.float32)
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            sub = build_graph(vectors, trusted).tocoo()
            dist = csr_matrix((sub.data, (idx[sub.row], idx[sub.col])), shape=(n, n))
            graph = self._make_graph(dist.maximum(dist.T).tocsr())
        if len(self._graphs) >= GRAPH_CACHE_SIZE:
            self._graphs.pop(next(iter(self._graphs)))
        self._graphs[chosen] = graph
        return graph

    # ---- search -------------------------------------------------------------------------
    def _build_search_index(self):
        self.keys = [node["key"] for node in self.nodes]
        self.key_tokens = [k.split() for k in self.keys]
        vocab, rows, cols = {}, [], []
        for r, key in enumerate(self.keys):
            for g in _grams(key):
                rows.append(r)
                cols.append(vocab.setdefault(g, len(vocab)))
        binary = csr_matrix((np.ones(len(rows), dtype=np.float32), (rows, cols)),
                            shape=(self.n, len(vocab)))
        df = np.asarray(binary.sum(axis=0)).ravel()
        self.gram_idf = np.log((1 + self.n) / (1 + df)) + 1.0
        self.gram_vocab = vocab
        self.gram_binary = binary
        weighted = binary.multiply(self.gram_idf).tocsr()
        norms = np.sqrt(np.asarray(weighted.multiply(weighted).sum(axis=1)).ravel())
        self.gram_tfidf = csr_matrix(weighted.multiply(1.0 / norms[:, None]))
        post = np.array([node["postings"] for node in self.nodes], dtype=float)
        self.popularity = np.log1p(post) / np.log1p(post.max())

    def search(self, query, limit=10, chosen=None):
        """Rank titles by typo-tolerant character n-gram match plus an all-words bonus."""
        grams = [g for g in _grams(query) if g in self.gram_vocab]
        if not _squash(query) or not grams:
            return []
        cols = np.array([self.gram_vocab[g] for g in grams])
        q = self.gram_idf[cols]
        coverage = (self.gram_binary[:, cols] @ q) / q.sum()
        cosine = (self.gram_tfidf[:, cols] @ q) / np.linalg.norm(q)
        words = query.lower().split()
        word_hit = np.fromiter(
            (all(any(t.startswith(w) for t in toks) for w in words) for toks in self.key_tokens),
            dtype=float, count=self.n)
        score = 0.6 * coverage + 0.4 * cosine + 0.15 * word_hit + 0.05 * self.popularity
        if chosen is not None:
            score[~self.active_mask(chosen)] = 0
        top = np.argsort(-score)[:limit]
        return [self.brief(int(i), chosen, score=float(score[i])) for i in top if score[i] > 0.15]

    # ---- node info ----------------------------------------------------------------------
    def brief(self, i, chosen=None, **extra):
        node = self.nodes[i]
        postings = node["postings"]
        if chosen is not None:   # count only postings in the selected countries
            postings = sum(v[0] for c, v in self.country_stats[i].items() if c in chosen)
        return {"id": i, "title": node["title"], "family": int(self.family[i]),
                "family_name": self.families[self.family[i]], "postings": postings, **extra}

    def detail(self, i, n_neighbors=8, chosen=None):
        dist = self.graph_for(chosen).dist
        start, end = dist.indptr[i], dist.indptr[i + 1]
        neighbors = dist.indices[start:end]
        order = np.argsort(dist.data[start:end])[:n_neighbors]
        info = {**self.brief(i, chosen), "companies": self.nodes[i]["companies"],
                "snippet": self.nodes[i]["snippet"],
                "neighbors": [self.brief(int(neighbors[j]), chosen, similarity=round(1 - float(dist.data[start + j]), 3))
                              for j in order]}
        if self.country_stats is not None:
            stats = self.country_stats[i]
            info["countries"] = [{"code": c, "name": country_name(c), "postings": v[0]}
                                 for c, v in sorted(stats.items(), key=lambda kv: -kv[1][0])][:6]
        return info

    def graph_payload(self):
        """Everything the 3D view needs to draw every node once."""
        upper = self.graph.dist.tocoo()
        keep = (upper.row < upper.col) & self.trusted[upper.row] & self.trusted[upper.col]
        edges = np.stack([upper.row[keep], upper.col[keep]], axis=1)
        return {
            "version": self.version,
            "families": self.families,
            "edges": edges.ravel().tolist(),
            "trusted": self.trusted.astype(int).tolist(),
            "pos": np.round(self.pos, 2).ravel().tolist(),
            "family": self.family.tolist(),
            "size": [round(float(p), 3) for p in self.popularity],
            "title": [node["title"] for node in self.nodes],
        }

    # ---- paths --------------------------------------------------------------------------
    def _route(self, matrix, src, dst):
        dist, pred = dijkstra(matrix, directed=False, indices=src, return_predecessors=True)
        if not np.isfinite(dist[dst]):
            return None
        route = [dst]
        while route[-1] != src:
            prev = int(pred[route[-1]])
            if prev < 0 or len(route) > self.n:
                return None
            route.append(prev)
        return route[::-1]

    def _describe(self, route, dist, chosen=None):
        steps, total, biggest = [], 0.0, 0.0
        for k, node in enumerate(route):
            step = self.brief(node, chosen)
            if k:
                d = float(dist[route[k - 1], node])
                step["similarity"] = round(1 - d, 3)
                total += d
                biggest = max(biggest, d)
            steps.append(step)
        return {"nodes": route, "steps": steps, "hops": len(route) - 1,
                "distance": round(total, 3), "largest_jump": round(biggest, 3)}

    def paths(self, src, dst, chosen=None):
        active = self.active_mask(chosen)
        if not (active[src] and active[dst]):
            raise ValueError("That job has no postings in the selected countries")
        graph = self.graph_for(chosen)
        if src == dst:
            same = self._describe([src], graph.dist, chosen)
            return {"same": True, "closest": same, "fewest": same}
        closest = self._route(graph.cost, src, dst)
        fewest = self._route(graph.hops, src, dst)
        if closest is None or fewest is None:
            raise ValueError("No route between these jobs within the selected countries")
        return {"same": False, "closest": self._describe(closest, graph.dist, chosen),
                "fewest": self._describe(fewest, graph.dist, chosen)}
