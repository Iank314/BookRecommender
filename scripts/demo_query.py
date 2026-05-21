# scripts/demo_query.py
from server.fetcher.fetcher import Fetcher, GOOGLE_ENDPOINT        # <— import the real Fetcher
from server.recommender.recommendation_engine import RecommendationEngine
from server.recommender.recommender import Recommender

def main() -> None:
    # Pull data live from Google Books
    fetcher = Fetcher(source=GOOGLE_ENDPOINT)      # no API key needed for light use
    engine  = RecommendationEngine()
    rec     = Recommender(fetcher, engine)

    # Build the index by querying Google Books
    rec.build(query="coming-of-age fantasy", max_results=40)

    # Free-text recommendation
    picks = rec.recommend_by_text("whimsical bittersweet adventure", top_n=5)
    for book, score in picks:
        print(f"{book.title} - {', '.join(book.authors)} ({score:.1f}%)")

if __name__ == "__main__":
    main()
