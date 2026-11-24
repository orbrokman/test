"""Deterministic L2-normalized text embedding and cosine similarity utilities."""

from __future__ import annotations

import hashlib
import math
import re

VECTOR_DIM = 128
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]+")


def compute_text_embedding(text: str, dim: int = VECTOR_DIM) -> list[float]:
    """Compute a deterministic L2-normalized dense vector embedding for text.

    Uses unigram and bigram feature hashing into a fixed-dimensional float
    vector so similarity search runs persistently in SQLite with zero external
    API calls in offline/CI environments.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return [0.0] * dim

    features: list[str] = list(tokens)
    for i in range(len(tokens) - 1):
        features.append(f"{tokens[i]}_{tokens[i + 1]}")

    vec = [0.0] * dim
    for feat in features:
        digest = hashlib.sha256(feat.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign

    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity clamped to ``[0.0, 1.0]``."""
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    return max(0.0, min(1.0, (dot + 1.0) / 2.0 if dot < 0.0 else dot))
