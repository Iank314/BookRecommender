"""Similarity engine: cosine over TF‑IDF + tag vectors."""

from __future__ import annotations

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

class RecommendationEngine:
    def __init__(self, metric: str = "cosine") -> None:
        if metric != "cosine":
            raise ValueError("Only cosine similarity is supported for now.")
        self.metric = metric
        self.feature_matrix: np.ndarray | None = None
        self.ids: list[str] | None = None
        self.sim_matrix: np.ndarray | None = None

    # ------------------------------------------------------------------
    # Building the index
    # ------------------------------------------------------------------
    def fit(self, feature_matrix: np.ndarray, ids: list[str]):
        """Store features and pre‑compute the all‑pairs similarity matrix."""
        self.ids = ids
        self.feature_matrix = feature_matrix
        self.sim_matrix = cosine_similarity(feature_matrix)

    # ------------------------------------------------------------------
    # Book‑to‑book recommendation
    # ------------------------------------------------------------------
    def recommend(self, book_id: str, top_n: int = 5) -> list[str]:
        if self.ids is None or self.sim_matrix is None:
            raise RuntimeError("Engine has not been fitted yet.")
        try:
            idx = self.ids.index(book_id)
        except ValueError:
            return []
        sims = self.sim_matrix[idx]
        best_idxs = np.argsort(sims)[::-1]
        best = [i for i in best_idxs if i != idx][:top_n]
        return [self.ids[i] for i in best]

    # ------------------------------------------------------------------
    # Free‑text / feature‑vector recommendation
    # ------------------------------------------------------------------
    def recommend_for_vector(self, query_vec: np.ndarray, top_n: int = 5) -> list[tuple[str, float]]:
        if self.feature_matrix is None or self.ids is None:
            raise RuntimeError("Engine has not been fitted yet.")
        sims = cosine_similarity([query_vec], self.feature_matrix)[0]
        best_idxs = np.argsort(sims)[::-1][:top_n]
        return [(self.ids[i], float(sims[i])) for i in best_idxs]
