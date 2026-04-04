# BookRecommender — Content-Based Book Recommender

## Next Features

- **User database** — persistent storage for user accounts and their saved books (across sessions)
- **Docker containerization** — package the app for easy deployment and consistent environments
- **Similarity scoring improvements** — refine the relevance algorithm for better recommendation quality

---

A full-stack book recommendation system that searches **Google Books** and **Open Library**, scores results by relevance and popularity, and suggests similar titles using TF-IDF cosine similarity. Users can build a personal library and get recommendations based on their saved collection.

---

## Tech Stack

| Layer | Technology | Why |
|-------|------------|-----|
| Backend | **Python 3.11, FastAPI, Uvicorn** | Async-ready API with automatic docs |
| Data & NLP | **NumPy, SciPy, scikit-learn** | TF-IDF vectorization, sparse matrices, cosine similarity |
| HTTP | **requests** | REST client for Google Books & Open Library APIs |
| Frontend | **HTML5, CSS3, vanilla JavaScript** | Lightweight single-page app, no framework overhead |
| Testing | **pytest, unittest** | Fast, readable unit and integration tests |

---

## Features

### Multi-Source Book Search
- Search by **title**, **author**, or **genre** across Google Books and Open Library
- Deduplicates results across APIs
- Scores books 0–100 using a hybrid formula: match quality + popularity metrics (edition count, ratings, want-to-read signals)
- Paginated results (20 per page) with relevance badges

### Content-Based Recommendations
- **Similar books**: given a book, fetches candidates matching its genres and ranks them by TF-IDF cosine similarity
- **Free-text recommendations**: describe what you want ("whimsical bittersweet adventure") and get matched books
- **Library-based recommendations**: recommendations drawn from the genres and descriptions of your entire saved collection

### Personal Library
- Save and remove books from an in-memory library
- View all saved books in a dedicated tab
- Get recommendations based on your full collection

### Frontend
- Dark-themed single-page app with category tabs (Title / Author / Genre / My Library)
- Book cards with expandable descriptions, relevance scores (color-coded), and external links (Google Books, Open Library, Google search)
- "Find Similar" button on each result
- Pagination with numbered page buttons
- Loading spinner and error display

---

## How It Works

```
┌─────────────┐  query    ┌───────────────────────┐
│  Frontend   │─────────► │  FastAPI  (/search)   │
└─────────────┘           └───────────────────────┘
                                    │
                          ┌─────────┴─────────┐
                          ▼                   ▼
                   Google Books API    Open Library API
                          │                   │
                          └─────────┬─────────┘
                                    ▼
                          Deduplicate & Score
                                    │
                                    ▼
                          Return paginated results
```

**Recommendation pipeline:**

```
Fetch candidates  →  Preprocess descriptions  →  TF-IDF + tag one-hot encoding
     →  Cosine similarity matrix  →  Top-N similar books
```

---

## Project Structure

```
BookRecommender/
├── frontend/
│   ├── index.html          Single-page app shell
│   ├── app.js              Event handling & API calls
│   └── style.css           Dark theme, responsive layout
├── server/
│   ├── app.py              FastAPI REST API
│   ├── models/
│   │   ├── book.py         Book dataclass
│   │   └── library.py      In-memory collection + CRUD/search
│   ├── fetcher/
│   │   └── fetcher.py      Google Books + Open Library adapters
│   ├── preprocessing/
│   │   └── text_processor.py   HTML/URL stripping, lowercasing, cleanup
│   ├── features/
│   │   └── features.py     TF-IDF vectorizer + tag one-hot encoder
│   └── recommender/
│       ├── recommendation_engine.py   Cosine similarity engine
│       └── recommender.py            Full pipeline orchestrator
├── scripts/
│   └── demo_query.py       CLI demo
├── tests/
│   ├── test_engine.py
│   ├── test_pipeline.py
│   └── test_recommender_edge.py
├── requirements.txt
└── README.md
```

---

## API Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/` | Serve frontend |
| `POST` | `/search` | Search books by title, author, or genre |
| `POST` | `/similar` | Find books similar to a given book |
| `GET` | `/library` | List saved books |
| `POST` | `/library/add` | Save a book to library |
| `DELETE` | `/library/{book_id}` | Remove a book from library |
| `POST` | `/library/recommend` | Recommendations based on saved library |

---

## Running the App

```bash
# Install dependencies
python -m pip install -r requirements.txt

# Start the server
uvicorn server.app:app --reload

# Open http://localhost:8000 in your browser
```

### CLI Demo

```bash
python -m scripts.demo_query
```

### Run Tests

```bash
python -m pytest
```
