import struct
from datetime import datetime

import numpy as np
import requests

from .db import now_iso

class EmbeddingUnavailable(Exception):
    pass

def is_configured(cfg):
    return bool(cfg["embedding"].get("api_key"))

def embed_texts(cfg, texts, timeout=30):
    emb = cfg["embedding"]
    if not emb.get("api_key"):
        raise EmbeddingUnavailable("未配置 embedding API Key（config.json → embedding.api_key）")
    try:
        resp = requests.post(
            emb["base_url"],
            headers={"Authorization": f"Bearer {emb['api_key']}"},
            json={"model": emb["model"], "input": texts, "encoding_format": "float"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
    except EmbeddingUnavailable:
        raise
    except Exception as e:
        raise EmbeddingUnavailable(f"embedding API 调用失败：{e}")
    data.sort(key=lambda d: d.get("index", 0))
    return [np.asarray(d["embedding"], dtype=np.float32) for d in data]

def _to_blob(vec):
    return struct.pack(f"{len(vec)}f", *vec.tolist())

def _from_blob(blob):
    return np.frombuffer(blob, dtype=np.float32)

def store_embedding(conn, memory_id, vec, model):
    conn.execute(
        "INSERT INTO embeddings(memory_id, vector, model, dim, created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(memory_id) DO UPDATE SET vector=excluded.vector, model=excluded.model, "
        "dim=excluded.dim, created_at=excluded.created_at",
        (memory_id, _to_blob(vec), model, len(vec), now_iso()),
    )

def try_embed_memory(conn, cfg, memory_id, content):
    try:
        vec = embed_texts(cfg, [content])[0]
        store_embedding(conn, memory_id, vec, cfg["embedding"]["model"])
        return True
    except EmbeddingUnavailable:
        return False

def backfill(conn, cfg, batch=32):
    rows = conn.execute(
        "SELECT m.id, m.content FROM memories m LEFT JOIN embeddings e ON e.memory_id = m.id "
        "WHERE m.status = 'active' AND e.memory_id IS NULL"
    ).fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        try:
            vecs = embed_texts(cfg, [r["content"] for r in chunk])
        except EmbeddingUnavailable as e:
            return done, str(e)
        for r, v in zip(chunk, vecs):
            store_embedding(conn, r["id"], v, cfg["embedding"]["model"])
            done += 1
    return done, None

def vector_scores(conn, query_vec):
    rows = conn.execute(
        "SELECT e.memory_id, e.vector FROM embeddings e "
        "JOIN memories m ON m.id = e.memory_id WHERE m.status = 'active'"
    ).fetchall()
    qn = np.linalg.norm(query_vec)
    if qn == 0:
        return {}
    scores = {}
    for r in rows:
        v = _from_blob(r["vector"])
        n = np.linalg.norm(v)
        if n == 0:
            continue
        scores[r["memory_id"]] = float(np.dot(query_vec, v) / (qn * n))
    return scores
