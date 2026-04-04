# server/recommender/recommender.py (patched)

import uuid

from server.fetcher.fetcher import Fetcher
from server.models.library import Library
from server.preprocessing import Preprocessor
from server.features.features import FeatureExtractor
from server.recommender.recommendation_engine import RecommendationEngine

class Recommender:
    def __init__(self, fetcher: Fetcher, engine: RecommendationEngine):
        self.fetcher      = fetcher
        self.library      = Library()
        self.preprocessor = Preprocessor()
        self.extractor    = FeatureExtractor()
        self.engine       = engine

    # --------------------------------------------------------------
    # NOTE: *source* is now optional.  Most remote workflows ignore it
    # because the Fetcher already knows its endpoint; local‐file builds
    # will still pass a path.
    # --------------------------------------------------------------
    def build(self, source: str | None = None, **fetch_kwargs):
        """Populate the library and fit the similarity index.

        Parameters
        ----------
        source : str | None
            For *local* JSON builds, pass the file path.  For remote
            endpoints (Google Books / Open Library), leave this as None.
        **fetch_kwargs
            Passed straight through to `Fetcher.fetch(...)`, e.g.
            `query="fantasy", max_results=40`.
        """
        if source is not None and source != self.fetcher.source:
            # Allow callers to override the fetcher source (rare)
            self.fetcher.source = source

        # 1) Pull raw books
        books = self.fetcher.fetch(**fetch_kwargs)
        for b in books:
            self.library.add(b)

        # 2) Preprocess — combine title, tags, and description so all
        #    signals contribute to similarity scoring
        cleaned = {}
        for b in self.library.all():
            parts = [b.title, " ".join(b.tags), b.description]
            cleaned[b.id] = self.preprocessor.process(" ".join(parts))
        tags = {b.id: b.tags for b in self.library.all()}

        # 3) Vectorise & fit engine
        features, ids = self.extractor.fit_transform(cleaned, tags)
        self.engine.fit(features, ids)

    def recommend(self, book_id: str, top_n: int = 5):
        rec_ids = self.engine.recommend(book_id, top_n)
        return [self.library.get_by_id(i) for i in rec_ids]

    def recommend_by_text(self, query: str, top_n: int = 5):
        query = query.strip()
        if not query:
            return []
        qid    = f"__q_{uuid.uuid4().hex}__"
        q_desc = {qid: self.preprocessor.process(query)}
        q_tags = {qid: []}
        q_vec  = self.extractor.transform(q_desc, q_tags)
        scored = self.engine.recommend_for_vector(q_vec.toarray()[0], top_n)
        results = []
        for book_id, score in scored:
            book = self.library.get_by_id(book_id)
            if book:
                results.append((book, round(score * 100, 1)))
        return results
