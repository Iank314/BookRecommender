const API = "";  // same origin

const form      = document.getElementById("search-form");
const input     = document.getElementById("search-input");
const btn       = document.getElementById("search-btn");
const loading   = document.getElementById("loading");
const errorBox  = document.getElementById("error");
const results   = document.getElementById("results");
const heading   = document.getElementById("results-heading");
const container = document.getElementById("results-container");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = input.value.trim();
  if (!query) return;

  // UI state: loading
  btn.disabled = true;
  loading.classList.remove("hidden");
  results.classList.add("hidden");
  errorBox.classList.add("hidden");

  try {
    // 1. Build the index from Open Library using the search query
    const buildRes = await fetch(`${API}/build`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: query,
        max_results: 20,
        source: "https://openlibrary.org/search.json",
      }),
    });

    if (!buildRes.ok) {
      const err = await buildRes.json().catch(() => null);
      throw new Error(err?.detail || `Build failed (${buildRes.status})`);
    }

    // 2. Get recommendations based on the same query text
    const recRes = await fetch(
      `${API}/recommend?q=${encodeURIComponent(query)}&top_n=20`
    );

    if (!recRes.ok) {
      const err = await recRes.json().catch(() => null);
      throw new Error(err?.detail || `Recommend failed (${recRes.status})`);
    }

    const books = await recRes.json();
    renderResults(books, query);
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    loading.classList.add("hidden");
  }
});

function renderResults(books, query) {
  container.innerHTML = "";

  if (books.length === 0) {
    heading.textContent = "No results found.";
    results.classList.remove("hidden");
    return;
  }

  heading.textContent = `Top ${books.length} results for "${query}"`;

  // Group books by their primary tag (genre)
  const grouped = {};
  books.forEach((book, i) => {
    const genre = book.tags.length > 0 ? book.tags[0] : "Uncategorized";
    if (!grouped[genre]) grouped[genre] = [];
    grouped[genre].push({ ...book, rank: i + 1 });
  });

  // Render each genre section
  for (const [genre, genreBooks] of Object.entries(grouped)) {
    const section = document.createElement("div");
    section.className = "genre-section";

    const label = document.createElement("div");
    label.className = "genre-label";
    label.textContent = genre;
    section.appendChild(label);

    for (const book of genreBooks) {
      section.appendChild(createCard(book));
    }

    container.appendChild(section);
  }

  results.classList.remove("hidden");
}

function createCard(book) {
  const card = document.createElement("div");
  card.className = "book-card";

  const rank = document.createElement("div");
  rank.className = "book-rank";
  rank.textContent = book.rank;

  const info = document.createElement("div");
  info.className = "book-info";

  const title = document.createElement("div");
  title.className = "book-title";
  title.textContent = book.title;

  const authors = document.createElement("div");
  authors.className = "book-authors";
  authors.textContent = book.authors.length > 0
    ? book.authors.join(", ")
    : "Unknown author";

  const tags = document.createElement("div");
  tags.className = "book-tags";
  for (const t of book.tags.slice(0, 5)) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = t;
    tags.appendChild(tag);
  }

  info.appendChild(title);
  info.appendChild(authors);
  info.appendChild(tags);

  card.appendChild(rank);
  card.appendChild(info);

  // Relevance badge
  if (book.relevance != null) {
    const badge = document.createElement("div");
    badge.className = "relevance-badge";
    badge.textContent = `${book.relevance}%`;
    if (book.relevance >= 50) badge.classList.add("high");
    else if (book.relevance >= 20) badge.classList.add("mid");
    else badge.classList.add("low");
    card.appendChild(badge);
  }

  return card;
}
