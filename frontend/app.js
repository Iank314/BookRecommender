const API = "";

const form       = document.getElementById("search-form");
const input      = document.getElementById("search-input");
const btn        = document.getElementById("search-btn");
const loading    = document.getElementById("loading");
const errorBox   = document.getElementById("error");
const results    = document.getElementById("results");
const heading    = document.getElementById("results-heading");
const container  = document.getElementById("results-container");
const tabs       = document.querySelectorAll(".tab");
const pagination = document.getElementById("pagination");
const searchBar  = document.querySelector(".search-bar");

// Auth elements
const authStatus    = document.getElementById("auth-status");
const authLoginBtn  = document.getElementById("auth-login-btn");
const authLogoutBtn = document.getElementById("auth-logout-btn");
const authModal     = document.getElementById("auth-modal");
const authClose     = document.getElementById("auth-close");
const authTitle     = document.getElementById("auth-title");
const authForm      = document.getElementById("auth-form");
const authUsername  = document.getElementById("auth-username");
const authPassword  = document.getElementById("auth-password");
const authError     = document.getElementById("auth-error");
const authSubmit    = document.getElementById("auth-submit");
const authToggleText = document.getElementById("auth-toggle-text");
const authToggleBtn  = document.getElementById("auth-toggle-btn");

let activeCategory = "title";
let lastQuery = "";
let currentPage = 1;
let totalPages = 0;
let viewMode = "search"; // "search" | "library"
let libraryView = "saved"; // "saved" | "liked" | "disliked"
let currentUser = null;  // username string when logged in, else null
let authMode = "login";  // "login" | "register"

const placeholders = {
  title:  "Search by title... e.g. Harry Potter",
  author: "Search by author... e.g. J.K. Rowling",
  genre:  "Search by genre... e.g. comedy, sci-fi, romance",
};

// Tab switching
tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    tabs.forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    activeCategory = tab.dataset.category;

    if (activeCategory === "library") {
      searchBar.classList.add("hidden");
      viewMode = "library";
      loadLibrary();
    } else {
      searchBar.classList.remove("hidden");
      input.placeholder = placeholders[activeCategory];
      viewMode = "search";
      input.focus();
    }
  });
});

// ---- Auth ----

async function checkAuth() {
  try {
    const res = await fetch(`${API}/auth/me`);
    currentUser = res.ok ? (await res.json()).username : null;
  } catch {
    currentUser = null;
  }
  renderAuthBar();
}

function renderAuthBar() {
  if (currentUser) {
    authStatus.textContent = `Signed in as ${currentUser}`;
    authLoginBtn.classList.add("hidden");
    authLogoutBtn.classList.remove("hidden");
  } else {
    authStatus.textContent = "";
    authLoginBtn.classList.remove("hidden");
    authLogoutBtn.classList.add("hidden");
  }
}

function openAuth(mode = "login") {
  authMode = mode;
  updateAuthModal();
  authError.classList.add("hidden");
  authForm.reset();
  authModal.classList.remove("hidden");
  authUsername.focus();
}

function closeAuth() {
  authModal.classList.add("hidden");
}

function updateAuthModal() {
  const isLogin = authMode === "login";
  authTitle.textContent = isLogin ? "Log in" : "Sign up";
  authSubmit.textContent = isLogin ? "Log in" : "Create account";
  authToggleText.textContent = isLogin ? "Don't have an account?" : "Already have an account?";
  authToggleBtn.textContent = isLogin ? "Sign up" : "Log in";
  authPassword.autocomplete = isLogin ? "current-password" : "new-password";
}

authLoginBtn.addEventListener("click", () => openAuth("login"));
authClose.addEventListener("click", closeAuth);
authModal.addEventListener("click", (e) => {
  if (e.target === authModal) closeAuth();
});

authToggleBtn.addEventListener("click", () => {
  authMode = authMode === "login" ? "register" : "login";
  authError.classList.add("hidden");
  updateAuthModal();
});

authForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = authUsername.value.trim();
  const password = authPassword.value;
  if (!username || !password) return;
  const endpoint = authMode === "login" ? "/auth/login" : "/auth/register";
  authSubmit.disabled = true;
  try {
    const res = await fetch(`${API}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(data?.detail || "Authentication failed");
    currentUser = data.username;
    renderAuthBar();
    closeAuth();
    if (activeCategory === "library") loadLibrary();
  } catch (err) {
    authError.textContent = err.message;
    authError.classList.remove("hidden");
  } finally {
    authSubmit.disabled = false;
  }
});

authLogoutBtn.addEventListener("click", async () => {
  try { await fetch(`${API}/auth/logout`, { method: "POST" }); } catch {}
  currentUser = null;
  renderAuthBar();
  if (activeCategory === "library") loadLibrary();
});

checkAuth();

// Search form
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = input.value.trim();
  if (!query) return;
  lastQuery = query;
  currentPage = 1;
  await doSearch(query, activeCategory, 1);
});

// ---- Search ----

async function doSearch(query, category, page) {
  btn.disabled = true;
  showLoading("Searching and ranking books...");

  try {
    const res = await fetch(`${API}/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query, category, top_n: 100, page, page_size: 20,
      }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || `Search failed`);

    const data = await res.json();
    currentPage = data.page;
    totalPages = data.total_pages;
    renderResults(data.books, query, category, data.total, page);
    renderPagination(data.page, data.total_pages);
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    hideLoading();
  }
}

function renderResults(books, query, category, total, page) {
  container.innerHTML = "";
  pagination.classList.add("hidden");

  if (books.length === 0) {
    heading.textContent = "No results found.";
    results.classList.remove("hidden");
    return;
  }

  const label = { title: "title", author: "author", genre: "genre" }[category] || "search";
  const startRank = (page - 1) * 20;
  heading.textContent = `${total} results for ${label}: "${query}"`;

  renderBookList(books, startRank);
  results.classList.remove("hidden");
}

// ---- Library ----

async function loadLibrary() {
  if (!currentUser) { renderLoginPrompt(); return; }
  const labels = { saved: "Saved", liked: "Liked", disliked: "Disliked" };
  showLoading(`Loading your ${labels[libraryView].toLowerCase()} books...`);
  try {
    let url;
    if (libraryView === "saved") url = `${API}/library`;
    else url = `${API}/library/feedback?kind=${libraryView === "liked" ? "up" : "down"}`;

    const res = await fetch(url);
    if (res.status === 401) { currentUser = null; renderAuthBar(); renderLoginPrompt(); return; }
    if (!res.ok) throw new Error("Failed to load library");
    const books = await res.json();
    renderLibrary(books, labels[libraryView]);
  } catch (err) {
    showError(err.message);
  } finally {
    hideLoading();
  }
}

function renderLoginPrompt() {
  hideLoading();
  container.innerHTML = "";
  pagination.classList.add("hidden");
  heading.textContent = "Your personal library";

  const hint = document.createElement("p");
  hint.className = "library-hint";
  hint.textContent = "Log in to save books and build your personal library across devices.";
  container.appendChild(hint);

  const loginBtn = document.createElement("button");
  loginBtn.className = "recommend-btn library-login-btn";
  loginBtn.textContent = "Log in or sign up";
  loginBtn.addEventListener("click", () => openAuth("login"));
  container.appendChild(loginBtn);

  results.classList.remove("hidden");
}

function renderLibrary(books, label) {
  container.innerHTML = "";
  pagination.classList.add("hidden");

  // View switcher: Saved / Liked / Disliked. Clicking refetches the chosen
  // section. Active state is purely visual; libraryView is the source of truth.
  const tabs = document.createElement("div");
  tabs.className = "library-tabs";
  const switcherSpec = [
    { key: "saved", label: "Saved" },
    { key: "liked", label: "Liked" },
    { key: "disliked", label: "Disliked" },
  ];
  for (const { key, label: tabLabel } of switcherSpec) {
    const btn = document.createElement("button");
    btn.className = "library-tab" + (libraryView === key ? " active" : "");
    btn.textContent = tabLabel;
    btn.addEventListener("click", () => {
      if (libraryView === key) return;
      libraryView = key;
      loadLibrary();
    });
    tabs.appendChild(btn);
  }
  container.appendChild(tabs);

  if (books.length === 0) {
    heading.textContent = `${label} (0 books)`;
    const hint = document.createElement("p");
    hint.className = "library-hint";
    hint.textContent = libraryView === "saved"
      ? 'Search for books and click "Save to Library" to build your collection.'
      : libraryView === "liked"
        ? "Books you give a 👍 will appear here and boost similar recommendations."
        : "Books you give a 👎 will appear here and suppress similar recommendations.";
    container.appendChild(hint);
    results.classList.remove("hidden");
    return;
  }

  heading.textContent = `${label} (${books.length} books)`;

  // Recommend button only makes sense on the saved view — feedback alone
  // can't drive recs without a base library.
  if (libraryView === "saved") {
    const recBtn = document.createElement("button");
    recBtn.className = "recommend-btn";
    recBtn.textContent = "Get Recommendations Based on My Library";
    recBtn.addEventListener("click", getLibraryRecommendations);
    container.appendChild(recBtn);
  }

  // The Remove action differs by view: saved → DELETE /library/{id},
  // liked/disliked → DELETE /library/feedback/{id}.
  const removeAction = libraryView === "saved" ? removeFromLibrary : removeFeedback;
  renderBookList(books, 0, { showRemove: true, removeAction });
  results.classList.remove("hidden");
}

async function saveToLibrary(book) {
  try {
    const res = await fetch(`${API}/library/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: book.id, title: book.title, authors: book.authors,
        description: book.description || "", tags: book.tags, metadata: book.metadata || {},
      }),
    });
    if (!res.ok) throw new Error("Failed to save");
    return true;
  } catch {
    return false;
  }
}

async function removeFromLibrary(bookId) {
  try {
    const res = await fetch(`${API}/library/${encodeURIComponent(bookId)}`, { method: "DELETE" });
    if (!res.ok) throw new Error("Failed to remove");
    return true;
  } catch {
    return false;
  }
}

async function setFeedback(book, kind) {
  try {
    const res = await fetch(`${API}/library/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: book.id, title: book.title, authors: book.authors,
        description: book.description || "", tags: book.tags,
        metadata: book.metadata || {}, kind,
      }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function removeFeedback(bookId) {
  try {
    const res = await fetch(`${API}/library/feedback/${encodeURIComponent(bookId)}`, {
      method: "DELETE",
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function getLibraryRecommendations() {
  showLoading("Analyzing your library and finding recommendations...");
  try {
    const res = await fetch(`${API}/library/recommend?top_n=20`, { method: "POST" });
    if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Recommendation failed");
    const books = await res.json();
    renderLibraryRecommendations(books);
  } catch (err) {
    showError(err.message);
  } finally {
    hideLoading();
  }
}

function renderLibraryRecommendations(books) {
  container.innerHTML = "";
  pagination.classList.add("hidden");

  // Back button
  const backBtn = document.createElement("button");
  backBtn.className = "back-btn";
  backBtn.textContent = "\u2190 Back to My Library";
  backBtn.addEventListener("click", loadLibrary);
  container.appendChild(backBtn);

  if (books.length === 0) {
    heading.textContent = "No recommendations found. Try saving more books with varied genres.";
    results.classList.remove("hidden");
    return;
  }

  heading.textContent = `Recommended for you (${books.length} books)`;
  renderBookList(books, 0);
  results.classList.remove("hidden");
}

// ---- Find Similar ----

async function findSimilar(book) {
  showLoading("Finding similar books...");
  try {
    const res = await fetch(`${API}/similar`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: book.title, authors: book.authors,
        description: book.description || "", tags: book.tags, top_n: 20,
      }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Similar search failed");
    const books = await res.json();
    renderSimilarResults(books, book.title);
  } catch (err) {
    showError(err.message);
  } finally {
    hideLoading();
  }
}

function renderSimilarResults(books, sourceTitle) {
  container.innerHTML = "";
  pagination.classList.add("hidden");

  const backBtn = document.createElement("button");
  backBtn.className = "back-btn";
  backBtn.textContent = "\u2190 Back to search results";
  backBtn.addEventListener("click", () => {
    if (lastQuery) doSearch(lastQuery, activeCategory, currentPage);
    else results.classList.add("hidden");
  });
  container.appendChild(backBtn);

  if (books.length === 0) {
    heading.textContent = `No similar books found for "${sourceTitle}"`;
    results.classList.remove("hidden");
    return;
  }

  heading.textContent = `Books similar to "${sourceTitle}"`;
  renderBookList(books, 0);
  results.classList.remove("hidden");
}

// ---- Shared rendering ----

function renderBookList(books, startRank, options = {}) {
  const grouped = {};
  books.forEach((book, i) => {
    const genre = book.tags.length > 0 ? book.tags[0] : "Uncategorized";
    if (!grouped[genre]) grouped[genre] = [];
    grouped[genre].push({ ...book, rank: startRank + i + 1 });
  });

  for (const [genre, genreBooks] of Object.entries(grouped)) {
    const section = document.createElement("div");
    section.className = "genre-section";

    const genreLabel = document.createElement("div");
    genreLabel.className = "genre-label";
    genreLabel.textContent = genre;
    section.appendChild(genreLabel);

    for (const book of genreBooks) {
      section.appendChild(createCard(book, options));
    }
    container.appendChild(section);
  }
}

function renderPagination(page, pages) {
  pagination.innerHTML = "";
  if (pages <= 1) { pagination.classList.add("hidden"); return; }

  const prev = document.createElement("button");
  prev.className = "page-btn";
  prev.textContent = "Prev";
  prev.disabled = page <= 1;
  prev.addEventListener("click", () => doSearch(lastQuery, activeCategory, page - 1));
  pagination.appendChild(prev);

  for (let i = 1; i <= pages; i++) {
    const b = document.createElement("button");
    b.className = "page-btn" + (i === page ? " active" : "");
    b.textContent = i;
    b.addEventListener("click", () => doSearch(lastQuery, activeCategory, i));
    pagination.appendChild(b);
  }

  const next = document.createElement("button");
  next.className = "page-btn";
  next.textContent = "Next";
  next.disabled = page >= pages;
  next.addEventListener("click", () => doSearch(lastQuery, activeCategory, page + 1));
  pagination.appendChild(next);

  pagination.classList.remove("hidden");
}

function createCard(book, options = {}) {
  const card = document.createElement("div");
  card.className = "book-card";

  const rank = document.createElement("div");
  rank.className = "book-rank";
  rank.textContent = book.rank;

  const info = document.createElement("div");
  info.className = "book-info";

  const header = document.createElement("div");
  header.className = "book-header";

  const title = document.createElement("div");
  title.className = "book-title";
  title.textContent = book.title;

  const authors = document.createElement("div");
  authors.className = "book-authors";
  authors.textContent = book.authors.length > 0 ? book.authors.join(", ") : "Unknown author";

  const tags = document.createElement("div");
  tags.className = "book-tags";
  for (const t of book.tags.slice(0, 5)) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = t;
    tags.appendChild(tag);
  }

  header.appendChild(title);
  header.appendChild(authors);
  header.appendChild(tags);
  info.appendChild(header);

  // Expandable details
  const details = document.createElement("div");
  details.className = "book-details hidden";

  if (book.description) {
    const desc = document.createElement("p");
    desc.className = "book-description";
    desc.textContent = book.description;
    details.appendChild(desc);
  }

  // Links
  const links = document.createElement("div");
  links.className = "book-links";

  const meta = book.metadata || {};
  if (meta.infoLink) {
    const gbLink = document.createElement("a");
    gbLink.href = meta.infoLink;
    gbLink.target = "_blank";
    gbLink.rel = "noopener";
    gbLink.className = "book-link";
    gbLink.textContent = "Google Books";
    links.appendChild(gbLink);
  }
  if (book.id && book.id.startsWith("ol_")) {
    const olLink = document.createElement("a");
    olLink.href = `https://openlibrary.org${book.id.replace("ol_", "")}`;
    olLink.target = "_blank";
    olLink.rel = "noopener";
    olLink.className = "book-link";
    olLink.textContent = "Open Library";
    links.appendChild(olLink);
  }
  const searchQuery = encodeURIComponent(`${book.title} ${book.authors.join(" ")} read online`);
  const googleLink = document.createElement("a");
  googleLink.href = `https://www.google.com/search?q=${searchQuery}`;
  googleLink.target = "_blank";
  googleLink.rel = "noopener";
  googleLink.className = "book-link";
  googleLink.textContent = "Find Online";
  links.appendChild(googleLink);

  details.appendChild(links);

  // Action buttons row
  const actions = document.createElement("div");
  actions.className = "book-actions";

  // Save to Library button
  if (!options.showRemove) {
    const saveBtn = document.createElement("button");
    saveBtn.className = "save-btn";
    saveBtn.textContent = "Save to Library";
    saveBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!currentUser) { openAuth("login"); return; }
      const ok = await saveToLibrary(book);
      if (ok) {
        saveBtn.textContent = "Saved!";
        saveBtn.disabled = true;
        saveBtn.classList.add("saved");
      }
    });
    actions.appendChild(saveBtn);
  }

  // Remove button — wired to whatever action the caller passed
  // (removeFromLibrary for saved, removeFeedback for liked/disliked).
  if (options.showRemove) {
    const remover = options.removeAction || removeFromLibrary;
    const removeBtn = document.createElement("button");
    removeBtn.className = "remove-btn";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const ok = await remover(book.id);
      if (ok) {
        card.style.opacity = "0.3";
        removeBtn.textContent = "Removed";
        removeBtn.disabled = true;
      }
    });
    actions.appendChild(removeBtn);
  }

  // Find Similar button
  const similarBtn = document.createElement("button");
  similarBtn.className = "similar-btn";
  similarBtn.textContent = "Find Similar";
  similarBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    findSimilar(book);
  });
  actions.appendChild(similarBtn);

  // Thumbs-up / thumbs-down. Server is idempotent; clicking the active
  // direction clears the feedback, clicking the other direction flips it.
  // `book.kind` is set by /library/feedback list responses; rec/search/
  // similar responses leave it undefined and the buttons start neutral.
  if (currentUser) {
    let currentKind = book.kind || null;

    const likeBtn = document.createElement("button");
    const dislikeBtn = document.createElement("button");
    const paint = () => {
      likeBtn.classList.toggle("active", currentKind === "up");
      dislikeBtn.classList.toggle("active", currentKind === "down");
    };

    likeBtn.className = "feedback-btn like-btn";
    likeBtn.textContent = "👍";
    likeBtn.title = "I like books like this — boost similar recommendations";
    likeBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const target = currentKind === "up" ? null : "up";
      const ok = target ? await setFeedback(book, "up") : await removeFeedback(book.id);
      if (ok) { currentKind = target; paint(); }
    });

    dislikeBtn.className = "feedback-btn dislike-btn";
    dislikeBtn.textContent = "👎";
    dislikeBtn.title = "Don't recommend books like this";
    dislikeBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const target = currentKind === "down" ? null : "down";
      const ok = target ? await setFeedback(book, "down") : await removeFeedback(book.id);
      if (ok) { currentKind = target; paint(); }
    });

    paint();
    actions.appendChild(likeBtn);
    actions.appendChild(dislikeBtn);
  }

  details.appendChild(actions);
  info.appendChild(details);

  // Toggle
  const toggle = document.createElement("div");
  toggle.className = "book-expand";
  toggle.textContent = "\u25BC";
  card.addEventListener("click", (e) => {
    if (e.target.closest("a, button")) return;
    const open = !details.classList.contains("hidden");
    details.classList.toggle("hidden");
    toggle.textContent = open ? "\u25BC" : "\u25B2";
    card.classList.toggle("expanded", !open);
  });

  card.appendChild(rank);
  card.appendChild(info);
  card.appendChild(toggle);

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

// ---- UI helpers ----

function showLoading(msg) {
  loading.querySelector("p").textContent = msg || "Loading...";
  loading.classList.remove("hidden");
  results.classList.add("hidden");
  errorBox.classList.add("hidden");
  pagination.classList.add("hidden");
}

function hideLoading() {
  loading.classList.add("hidden");
}

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.classList.remove("hidden");
}
