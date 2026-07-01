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
const yearFilter = document.querySelector(".year-filter");
const yearFrom   = document.getElementById("year-from");
const yearTo     = document.getElementById("year-to");
const themeToggle = document.getElementById("theme-toggle");

// Auth elements
const authStatus    = document.getElementById("auth-status");
const adminStatsBtn = document.getElementById("admin-stats-btn");
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
let currentIsAdmin = false;
let authMode = "login";  // "login" | "register"

// Sections state (saved view only). `sections` mirrors GET /library/sections,
// `activeSectionId` filters the saved view (null = all books), and
// `selectedBookIds` drives "recommend from these picked books".
let sections = [];            // [{id, name, book_ids}]
let activeSectionId = null;
let activeStatus = null;      // "want_to_read" | "reading" | "read" | null
let selectedBookIds = new Set();
let selectedBar = null;       // "Recommend from N selected" bar, re-created per render
// Local mirror of the saved view, so status/section changes can update chips
// and headings in place instead of refetching and re-rendering everything
// (which collapsed expanded cards and jumped the scroll position).
let libraryBooks = [];
let currentTitleLabel = "";
let currentViewCount = 0;

// Reading statuses render as built-in chips alongside user sections; a book
// has at most one status (exclusive), unlike sections (many-to-many).
const READING_STATUSES = [
  { key: "want_to_read", label: "Want to Read", emoji: "📥" },
  { key: "reading",      label: "Reading",      emoji: "📖" },
  { key: "read",         label: "Read",         emoji: "✅" },
];

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
      yearFilter.classList.add("hidden");
      viewMode = "library";
      loadLibrary();
    } else {
      searchBar.classList.remove("hidden");
      yearFilter.classList.remove("hidden");
      input.placeholder = placeholders[activeCategory];
      viewMode = "search";
      input.focus();
    }
  });
});

// ---- Theme (dark default, light opt-in, persisted) ----

function applyTheme(light) {
  document.body.classList.toggle("light", light);
  themeToggle.textContent = light ? "🌙" : "☀️";
  themeToggle.title = light ? "Switch to dark mode" : "Switch to light mode";
  try { localStorage.setItem("bookrec-theme", light ? "light" : "dark"); } catch {}
}

themeToggle.addEventListener("click", () => {
  applyTheme(!document.body.classList.contains("light"));
});

try { applyTheme(localStorage.getItem("bookrec-theme") === "light"); } catch {}

// ---- Genre accent colors (display only) ----

const GENRE_ACCENTS = [
  [/romance|romantasy|love/i, "#ec5f87"],
  [/fantasy/i, "#a36bd6"],
  [/science fiction|sci-?fi|space/i, "#26c6da"],
  [/horror|ghost/i, "#e05252"],
  [/mystery|crime|detective|noir/i, "#7986cb"],
  [/thriller|suspense/i, "#ff8a5c"],
  [/litrpg|gamelit|isekai|xianxia|wuxia/i, "#9575ff"],
  [/young adult|juvenile|children/i, "#ffc94d"],
  [/histor/i, "#bd9272"],
  [/biograph|memoir|nonfiction|non-fiction/i, "#90a4ae"],
  [/adventure|western/i, "#81c784"],
];

function genreAccent(genre) {
  for (const [re, color] of GENRE_ACCENTS) {
    if (re.test(genre)) return color;
  }
  return null;
}

// ---- Auth ----

let startedAtLibrary = false;

async function checkAuth() {
  try {
    const res = await fetch(`${API}/auth/me`);
    if (res.ok) {
      const data = await res.json();
      currentUser = data.username;
      currentIsAdmin = !!data.is_admin;
    } else {
      currentUser = null;
      currentIsAdmin = false;
    }
  } catch {
    currentUser = null;
    currentIsAdmin = false;
  }
  renderAuthBar();
  // Returning logged-in users land on their library, not an empty search box.
  // Anonymous visitors keep the search view — they have no library yet.
  if (currentUser && !startedAtLibrary) {
    startedAtLibrary = true;
    const libTab = document.querySelector('.tab[data-category="library"]');
    if (libTab && viewMode === "search" && !lastQuery) libTab.click();
  }
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
  adminStatsBtn.classList.toggle("hidden", !(currentUser && currentIsAdmin));
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
  if (authMode === "register") {
    const problem = credentialProblem(username, password);
    if (problem) {
      authError.textContent = problem;
      authError.classList.remove("hidden");
      return;
    }
  }
  const endpoint = authMode === "login" ? "/auth/login" : "/auth/register";
  authSubmit.disabled = true;
  try {
    const res = await fetch(`${API}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(friendlyError(data?.detail));
    currentUser = data.username;
    currentIsAdmin = !!data.is_admin;
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
  currentIsAdmin = false;
  renderAuthBar();
  if (activeCategory === "library") loadLibrary();
});

// ---- Admin stats ----

adminStatsBtn.addEventListener("click", async () => {
  showLoading("Loading stats...");
  try {
    const res = await fetch(`${API}/admin/stats`);
    if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Failed to load stats");
    renderAdminStats(await res.json());
  } catch (err) {
    showError(err.message);
  } finally {
    hideLoading();
  }
});

function renderAdminStats(data) {
  container.innerHTML = "";
  pagination.classList.add("hidden");
  heading.textContent = `Admin stats — ${data.accounts_total} account${data.accounts_total === 1 ? "" : "s"}`;

  const ago = (ts) => {
    if (!ts) return "—";
    const s = data.now - ts;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  };
  const n = (obj, key) => (obj && obj[key]) || 0;

  // Summary cards: activity by kind over 24h / 7d / all-time.
  const cards = document.createElement("div");
  cards.className = "stats-cards";
  const a24 = data.activity.last_24h, a7 = data.activity.last_7d, all = data.activity.all_time;
  const mem = data.memory || {};
  const caches = data.caches || {};
  const memValue = mem.rss_mb != null ? `${mem.rss_mb} MB` : "—";
  const memLabel = mem.peak_rss_mb != null ? `Memory RSS (peak ${mem.peak_rss_mb} MB)` : "Memory RSS";
  const spec = [
    [`${data.active_users.last_24h} / ${data.active_users.last_7d}`, "Active users (24h / 7d)"],
    [`${n(a24, "search")} / ${n(a7, "search")} / ${n(all, "search")}`, "Searches (24h / 7d / all)"],
    [`${n(a24, "similar")} / ${n(a7, "similar")} / ${n(all, "similar")}`, "Find Similar (24h / 7d / all)"],
    [`${n(a24, "recommend")} / ${n(a7, "recommend")} / ${n(all, "recommend")}`, "Recommendations (24h / 7d / all)"],
    [`${data.anonymous_events.last_24h} / ${data.anonymous_events.last_7d}`, "Anonymous events (24h / 7d)"],
    [memValue, memLabel],
    [`${caches.recommendation ?? 0} / ${caches.similar ?? 0} / ${caches.fetcher ?? 0}`, "Cache entries (rec / similar / fetcher)"],
  ];
  for (const [value, label] of spec) {
    const card = document.createElement("div");
    card.className = "stats-card";
    const v = document.createElement("div");
    v.className = "stats-value";
    v.textContent = value;
    const l = document.createElement("div");
    l.className = "stats-label";
    l.textContent = label;
    card.appendChild(v);
    card.appendChild(l);
    cards.appendChild(card);
  }
  container.appendChild(cards);

  // Accounts table.
  const table = document.createElement("table");
  table.className = "stats-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const h of ["Username", "Registered", "Books saved", "Last active"]) {
    const th = document.createElement("th");
    th.textContent = h;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const acct of data.accounts) {
    const tr = document.createElement("tr");
    const cells = [
      acct.username + (acct.is_admin ? " 👑" : ""),
      ago(acct.created_at),
      String(acct.books_saved),
      ago(acct.last_active),
    ];
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  container.appendChild(table);

  const note = document.createElement("p");
  note.className = "library-hint";
  note.textContent = "Activity tracking counts searches (first page only), Find Similar clicks, "
    + "and recommendation runs. Events started being recorded when this feature was deployed; "
    + "\"Last active\" reflects tracked events only.";
  container.appendChild(note);

  results.classList.remove("hidden");
}

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
    const yf = parseInt(yearFrom.value, 10);
    const yt = parseInt(yearTo.value, 10);
    const res = await fetch(`${API}/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query, category, top_n: 100, page, page_size: 20,
        year_from: Number.isFinite(yf) ? yf : null,
        year_to: Number.isFinite(yt) ? yt : null,
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

    const [res, secRes] = await Promise.all([
      fetch(url),
      libraryView === "saved" ? fetch(`${API}/library/sections`) : Promise.resolve(null),
    ]);
    if (res.status === 401) { currentUser = null; renderAuthBar(); renderLoginPrompt(); return; }
    if (!res.ok) throw new Error("Failed to load library");
    const books = await res.json();
    if (secRes && secRes.ok) sections = await secRes.json();
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
  selectedBookIds.clear();
  selectedBar = null;

  // View switcher: Saved / Liked / Disliked. Clicking refetches the chosen
  // view. Active state is purely visual; libraryView is the source of truth.
  const tabs = document.createElement("div");
  tabs.className = "library-views";
  const switcherSpec = [
    { key: "saved", label: "Saved" },
    { key: "liked", label: "Liked" },
    { key: "disliked", label: "Disliked" },
  ];
  for (const { key, label: tabLabel } of switcherSpec) {
    const btn = document.createElement("button");
    btn.className = "library-view" + (libraryView === key ? " active" : "");
    btn.textContent = tabLabel;
    btn.addEventListener("click", () => {
      if (libraryView === key) return;
      libraryView = key;
      loadLibrary();
    });
    tabs.appendChild(btn);
  }
  container.appendChild(tabs);

  // Section/status bar + filtering (saved view only). The active section may
  // have been deleted since the last render — fall back to All Books. Section
  // and status filters are mutually exclusive (picking one clears the other).
  let viewBooks = books;
  let activeSection = null;
  let statusInfo = null;
  if (libraryView === "saved") {
    libraryBooks = books;
    activeSection = sections.find((s) => s.id === activeSectionId) || null;
    if (!activeSection) activeSectionId = null;
    statusInfo = READING_STATUSES.find((s) => s.key === activeStatus) || null;
    if (!statusInfo) activeStatus = null;
    container.appendChild(renderSectionBar(books));
    if (activeSection) {
      const memberIds = new Set(activeSection.book_ids);
      viewBooks = books.filter((b) => memberIds.has(b.id));
    } else if (statusInfo) {
      viewBooks = books.filter((b) => b.reading_status === statusInfo.key);
    }
  }

  const titleLabel = activeSection
    ? `${activeSection.name}`
    : statusInfo
      ? `${statusInfo.emoji} ${statusInfo.label}`
      : label;
  currentTitleLabel = titleLabel;
  currentViewCount = viewBooks.length;

  if (viewBooks.length === 0) {
    heading.textContent = `${titleLabel} (0 books)`;
    const hint = document.createElement("p");
    hint.className = "library-hint";
    hint.textContent = activeSection
      ? 'This section is empty. View All Books and use "Add to section…" on a book.'
      : statusInfo
        ? `No books marked "${statusInfo.label}" yet. Use the Status dropdown on a saved book.`
        : libraryView === "saved"
        ? 'Search for books and click "Save to Library" to build your collection.'
        : libraryView === "liked"
          ? "Books you give a 👍 will appear here and boost similar recommendations."
          : "Books you give a 👎 will appear here and suppress similar recommendations.";
    container.appendChild(hint);
    results.classList.remove("hidden");
    return;
  }

  heading.textContent = `${titleLabel} (${viewBooks.length} books)`;

  // Recommend button only makes sense on the saved view — feedback alone
  // can't drive recs without a base library. A selected section scopes the
  // recommendation to just its books.
  if (libraryView === "saved") {
    const recBtn = document.createElement("button");
    recBtn.className = "recommend-btn";
    recBtn.textContent = activeSection
      ? `Get Recommendations from "${activeSection.name}"`
      : statusInfo
        ? `Get Recommendations from "${statusInfo.label}" Books`
        : "Get Recommendations Based on My Library";
    // Status filters scope through book_ids — the backend doesn't need to
    // know about statuses, and the cache signature already covers the scope.
    const scopedIds = viewBooks.map((b) => b.id);
    recBtn.addEventListener("click", () => {
      if (activeSection) {
        getLibraryRecommendations({ section_id: activeSection.id }, activeSection.name);
      } else if (statusInfo) {
        getLibraryRecommendations({ book_ids: scopedIds }, `your "${statusInfo.label}" books`);
      } else {
        getLibraryRecommendations(null, null);
      }
    });
    container.appendChild(recBtn);

    // "Recommend from N selected" bar — hidden until a checkbox is ticked.
    selectedBar = document.createElement("button");
    selectedBar.className = "recommend-btn selected-bar hidden";
    selectedBar.addEventListener("click", () => {
      if (selectedBookIds.size === 0) return;
      getLibraryRecommendations(
        { book_ids: [...selectedBookIds] },
        `${selectedBookIds.size} selected book${selectedBookIds.size > 1 ? "s" : ""}`,
      );
    });
    container.appendChild(selectedBar);
  }

  // The Remove action differs by view: saved → DELETE /library/{id},
  // liked/disliked → DELETE /library/feedback/{id}.
  const removeAction = libraryView === "saved" ? removeFromLibrary : removeFeedback;
  renderBookList(viewBooks, 0, {
    showRemove: true,
    removeAction,
    sectionControls: libraryView === "saved",
    activeSection,
  });
  results.classList.remove("hidden");
}

// ---- Sections ----

function renderSectionBar(books) {
  const bar = document.createElement("div");
  bar.className = "section-bar";

  const allChip = document.createElement("button");
  allChip.className = "section-chip"
    + (activeSectionId === null && activeStatus === null ? " active" : "");
  allChip.textContent = `All Books (${books.length})`;
  allChip.addEventListener("click", () => {
    if (activeSectionId === null && activeStatus === null) return;
    activeSectionId = null;
    activeStatus = null;
    loadLibrary();
  });
  bar.appendChild(allChip);

  // Built-in reading-status chips — same affordance as sections, but driven
  // by each book's exclusive reading_status instead of memberships.
  for (const status of READING_STATUSES) {
    const count = books.filter((b) => b.reading_status === status.key).length;
    const chip = document.createElement("button");
    chip.className = "section-chip section-builtin"
      + (activeStatus === status.key ? " active" : "");
    chip.textContent = `${status.emoji} ${status.label} (${count})`;
    chip.addEventListener("click", () => {
      if (activeStatus === status.key) return;
      activeStatus = status.key;
      activeSectionId = null;
      loadLibrary();
    });
    bar.appendChild(chip);
  }

  for (const section of sections) {
    const chip = document.createElement("button");
    chip.className = "section-chip" + (activeSectionId === section.id ? " active" : "");
    chip.textContent = `${section.name} (${section.book_ids.length})`;
    chip.addEventListener("click", () => {
      if (activeSectionId === section.id) return;
      activeSectionId = section.id;
      activeStatus = null;
      loadLibrary();
    });
    bar.appendChild(chip);
  }

  const newBtn = document.createElement("button");
  newBtn.className = "section-chip section-new";
  newBtn.textContent = "+ New Section";
  newBtn.addEventListener("click", async () => {
    const name = prompt("Section name (e.g. Sci-fi favorites):");
    if (!name || !name.trim()) return;
    const created = await createSection(name.trim());
    if (created) { activeSectionId = created.id; loadLibrary(); }
  });
  bar.appendChild(newBtn);

  // Rename / delete controls for the open section.
  const active = sections.find((s) => s.id === activeSectionId);
  if (active) {
    const renameBtn = document.createElement("button");
    renameBtn.className = "section-chip section-manage";
    renameBtn.textContent = "Rename";
    renameBtn.addEventListener("click", async () => {
      const name = prompt(`Rename "${active.name}" to:`, active.name);
      if (!name || !name.trim() || name.trim() === active.name) return;
      if (await renameSection(active.id, name.trim())) loadLibrary();
    });
    bar.appendChild(renameBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.className = "section-chip section-manage section-delete";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", async () => {
      if (!confirm(`Delete the section "${active.name}"? Books stay in your library.`)) return;
      if (await deleteSection(active.id)) { activeSectionId = null; loadLibrary(); }
    });
    bar.appendChild(deleteBtn);
  }

  return bar;
}

// In-place refresh helpers — status/section changes update the chip bar and
// heading without refetching, so expanded cards and scroll position survive.
function refreshSectionBar() {
  if (libraryView !== "saved") return;
  const old = container.querySelector(".section-bar");
  if (old) old.replaceWith(renderSectionBar(libraryBooks));
}

function removeCardFromView(card) {
  const group = card.closest(".genre-section");
  card.remove();
  if (group && !group.querySelector(".book-card")) group.remove();
  currentViewCount -= 1;
  heading.textContent = `${currentTitleLabel} (${currentViewCount} books)`;
}

// Reading-status dropdown on saved-book cards. Selecting the current status
// again is a no-op; "No status" clears it.
function buildStatusSelect(book, card) {
  const select = document.createElement("select");
  select.className = "section-select";
  const placeholder = document.createElement("option");
  placeholder.value = "__none__";
  placeholder.textContent = book.reading_status ? "No status" : "Status…";
  select.appendChild(placeholder);
  for (const status of READING_STATUSES) {
    const opt = document.createElement("option");
    opt.value = status.key;
    opt.textContent = `${status.emoji} ${status.label}`;
    select.appendChild(opt);
  }
  if (book.reading_status) select.value = book.reading_status;
  select.addEventListener("click", (e) => e.stopPropagation());
  select.addEventListener("change", async (e) => {
    e.stopPropagation();
    const value = select.value === "__none__" ? null : select.value;
    if (value === (book.reading_status || null)) return;
    if (!(await setReadingStatus(book.id, value))) {
      select.value = book.reading_status || "__none__";
      return;
    }
    // Update local state in place — no refetch, no re-render.
    book.reading_status = value;
    const original = libraryBooks.find((b) => b.id === book.id);
    if (original) original.reading_status = value;
    placeholder.textContent = value ? "No status" : "Status…";
    // Inside a status shelf, a book whose status changed no longer belongs.
    if (activeStatus && value !== activeStatus) removeCardFromView(card);
    refreshSectionBar();
  });
  return select;
}

async function setReadingStatus(bookId, status) {
  try {
    const res = await fetch(`${API}/library/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ book_id: bookId, status }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

// Dropdown used on saved-book cards. currentSection null → "Add to section…"
// (plain add, a book can sit in several sections); set → "Move to section…"
// (atomic add-to-target + remove-from-current on the server).
function buildSectionSelect(book, currentSection, card) {
  const select = document.createElement("select");
  select.className = "section-select";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = currentSection ? "Move to section…" : "Add to section…";
  select.appendChild(placeholder);
  for (const s of sections) {
    if (currentSection && s.id === currentSection.id) continue; // already here
    const opt = document.createElement("option");
    opt.value = String(s.id);
    opt.textContent = s.book_ids.includes(book.id) ? `${s.name} ✓` : s.name;
    if (s.book_ids.includes(book.id)) opt.disabled = true;
    select.appendChild(opt);
  }
  const newOpt = document.createElement("option");
  newOpt.value = "__new__";
  newOpt.textContent = "+ New section…";
  select.appendChild(newOpt);
  select.addEventListener("click", (e) => e.stopPropagation());
  select.addEventListener("change", async (e) => {
    e.stopPropagation();
    const value = select.value;
    select.value = "";
    if (!value) return;
    let sectionId = value;
    if (value === "__new__") {
      const name = prompt("Section name (e.g. Sci-fi favorites):");
      if (!name || !name.trim()) return;
      const created = await createSection(name.trim());
      if (!created) return;
      sections.push(created);
      sectionId = created.id;
    }
    const ok = await addBookToSection(
      Number(sectionId), book.id, currentSection ? currentSection.id : null,
    );
    if (!ok) return;
    // Update local membership and just the affected DOM — no refetch.
    const target = sections.find((s) => s.id === Number(sectionId));
    if (target && !target.book_ids.includes(book.id)) target.book_ids.push(book.id);
    if (currentSection) {
      const cur = sections.find((s) => s.id === currentSection.id);
      if (cur) cur.book_ids = cur.book_ids.filter((id) => id !== book.id);
      removeCardFromView(card); // it moved out of the shelf being viewed
    } else {
      select.replaceWith(buildSectionSelect(book, currentSection, card)); // show the ✓
    }
    refreshSectionBar();
  });
  return select;
}

function updateSelectedBar() {
  if (!selectedBar) return;
  const n = selectedBookIds.size;
  selectedBar.textContent = `Get Recommendations from ${n} Selected Book${n === 1 ? "" : "s"}`;
  selectedBar.classList.toggle("hidden", n === 0);
}

async function createSection(name) {
  try {
    const res = await fetch(`${API}/library/sections`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) { showError(data?.detail || "Failed to create section"); return null; }
    return data;
  } catch {
    return null;
  }
}

async function renameSection(sectionId, name) {
  try {
    const res = await fetch(`${API}/library/sections/${sectionId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      showError(data?.detail || "Failed to rename section");
    }
    return res.ok;
  } catch {
    return false;
  }
}

async function deleteSection(sectionId) {
  try {
    const res = await fetch(`${API}/library/sections/${sectionId}`, { method: "DELETE" });
    return res.ok;
  } catch {
    return false;
  }
}

// fromSectionId null = plain add; set = atomic move out of that section.
async function addBookToSection(sectionId, bookId, fromSectionId = null) {
  try {
    const res = await fetch(`${API}/library/sections/${sectionId}/books`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ book_id: bookId, from_section_id: fromSectionId }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function removeBookFromSection(sectionId, bookId) {
  try {
    const res = await fetch(
      `${API}/library/sections/${sectionId}/books/${encodeURIComponent(bookId)}`,
      { method: "DELETE" },
    );
    return res.ok;
  } catch {
    return false;
  }
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

async function getLibraryRecommendations(scope = null, scopeLabel = null) {
  // scope: null (whole library), {section_id} or {book_ids} \u2014 mirrors the
  // optional body /library/recommend accepts.
  showLoading(
    scopeLabel
      ? `Finding recommendations based on ${scopeLabel}...`
      : "Analyzing your library and finding recommendations...",
  );
  try {
    const res = await fetch(`${API}/library/recommend?top_n=20`, {
      method: "POST",
      ...(scope
        ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(scope) }
        : {}),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Recommendation failed");
    const books = await res.json();
    renderLibraryRecommendations(books, scopeLabel);
  } catch (err) {
    showError(err.message);
  } finally {
    hideLoading();
  }
}

function renderLibraryRecommendations(books, scopeLabel = null) {
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

  heading.textContent = scopeLabel
    ? `Recommended based on ${scopeLabel} (${books.length} books)`
    : `Recommended for you (${books.length} books)`;
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
        id: book.id || "", title: book.title, authors: book.authors,
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
    const genre = book.tags.length > 0 ? book.tags[0] : "Other";
    if (!grouped[genre]) grouped[genre] = [];
    grouped[genre].push({ ...book, rank: startRank + i + 1 });
  });

  for (const [genre, genreBooks] of Object.entries(grouped)) {
    const section = document.createElement("div");
    section.className = "genre-section";

    const genreLabel = document.createElement("div");
    genreLabel.className = "genre-label";
    genreLabel.textContent = genre;
    // Tint the group toward its genre — romance reads pink, sci-fi cyan, etc.
    const accent = genreAccent(genre);
    if (accent) {
      genreLabel.style.color = accent;
      section.style.borderLeft = `3px solid ${accent}`;
      section.style.paddingLeft = "0.8rem";
    }
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

  // Cover image (when the provider supplied one) — floats left so the
  // description wraps around it. Broken/missing images remove themselves.
  const thumbUrl = (book.metadata || {}).thumbnail;
  if (thumbUrl) {
    const cover = document.createElement("img");
    cover.className = "book-cover";
    cover.src = thumbUrl;
    cover.alt = `Cover of ${book.title}`;
    cover.loading = "lazy";
    cover.addEventListener("error", () => cover.remove());
    details.appendChild(cover);
  }

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

  // Section controls (saved library view only). In All Books: a dropdown to
  // file the book into a section. Inside a section: a dropdown to MOVE it to
  // a different section, plus a button to drop it from the current one.
  if (options.sectionControls) {
    const current = options.activeSection || null;
    actions.appendChild(buildStatusSelect(book, card));
    actions.appendChild(buildSectionSelect(book, current, card));
    if (current) {
      const sectionRemoveBtn = document.createElement("button");
      sectionRemoveBtn.className = "remove-btn";
      sectionRemoveBtn.textContent = `Remove from "${current.name}"`;
      sectionRemoveBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!(await removeBookFromSection(current.id, book.id))) return;
        const cur = sections.find((s) => s.id === current.id);
        if (cur) cur.book_ids = cur.book_ids.filter((id) => id !== book.id);
        removeCardFromView(card);
        refreshSectionBar();
      });
      actions.appendChild(sectionRemoveBtn);
    }
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

  // Selection checkbox for "recommend from these picked books" — visible
  // without expanding the card, saved library view only.
  if (options.sectionControls) {
    const selectBox = document.createElement("input");
    selectBox.type = "checkbox";
    selectBox.className = "select-box";
    selectBox.title = "Select for recommendations";
    selectBox.checked = selectedBookIds.has(book.id);
    selectBox.addEventListener("click", (e) => e.stopPropagation());
    selectBox.addEventListener("change", () => {
      if (selectBox.checked) selectedBookIds.add(book.id);
      else selectedBookIds.delete(book.id);
      updateSelectedBar();
    });
    card.appendChild(selectBox);
  }

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

// Account rules — keep in sync with the server's Username/Password constraints
// in server/app.py (constr min_length/max_length).
const USERNAME_MIN = 2, USERNAME_MAX = 32;
const PASSWORD_MIN = 6, PASSWORD_MAX = 128;

// Returns a friendly message if the credentials break a rule, else null.
function credentialProblem(username, password) {
  if (username.length < USERNAME_MIN)
    return `Sorry, the username has to be at least ${USERNAME_MIN} characters.`;
  if (username.length > USERNAME_MAX)
    return `Sorry, the username can be at most ${USERNAME_MAX} characters.`;
  if (password.length < PASSWORD_MIN)
    return `Sorry, the password has to be at least ${PASSWORD_MIN} characters.`;
  if (password.length > PASSWORD_MAX)
    return `Sorry, the password can be at most ${PASSWORD_MAX} characters.`;
  return null;
}

// Turn a server error `detail` into a readable message. FastAPI returns a
// string for our own HTTPExceptions (409 taken, 401 bad login, 429 throttle)
// but an array of validation objects for 422s — translate those into the same
// friendly wording instead of letting them stringify to "[object Object]".
function friendlyError(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    for (const e of detail) {
      const field = e?.loc?.[e.loc.length - 1];
      if (field === "username" || field === "password") {
        const label = field === "username" ? "username" : "password";
        if (e.type === "string_too_short") {
          const min = field === "username" ? USERNAME_MIN : PASSWORD_MIN;
          return `Sorry, the ${label} has to be at least ${min} characters.`;
        }
        if (e.type === "string_too_long") {
          const max = field === "username" ? USERNAME_MAX : PASSWORD_MAX;
          return `Sorry, the ${label} can be at most ${max} characters.`;
        }
      }
      if (e?.msg) return e.msg;
    }
  }
  return "Authentication failed.";
}
