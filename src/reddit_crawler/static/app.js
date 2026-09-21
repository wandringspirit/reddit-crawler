/* Reddit Crawler panel — vanilla JS, talks to the local FastAPI backend. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "reddit-crawler-form-v1";
  const PAGE_SIZE = 200;

  const state = {
    status: null,
    keywordSets: [],
    currentSetId: "",
    preset: "day",
    runId: null,
    job: null,
    run: null,
    results: [],
    resultsVersion: -1,
    pollTimer: null,
    shown: PAGE_SIZE,
    tz: (Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC",
  };

  // ------------------------------------------------------------------ utils
  async function api(path, options = {}) {
    const init = { method: options.method || "GET", headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, init);
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    if (!response.ok) {
      const message = payload && (payload.error || payload.detail) ? (payload.error || JSON.stringify(payload.detail)) : `HTTP ${response.status}`;
      throw new Error(message);
    }
    return payload;
  }

  function escapeHtml(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function highlight(text, spans, offset = 0, length = null) {
    // Render `text` (a slice starting at `offset` of the original) with <mark> around spans.
    text = text || "";
    const end = length === null ? offset + text.length : offset + length;
    let html = "";
    let cursor = offset;
    for (const [s, e] of spans || []) {
      const start = Math.max(s, offset), stop = Math.min(e, end);
      if (stop <= start) continue;
      if (start > cursor) html += escapeHtml(text.slice(cursor - offset, start - offset));
      html += "<mark>" + escapeHtml(text.slice(start - offset, stop - offset)) + "</mark>";
      cursor = stop;
    }
    if (cursor < end) html += escapeHtml(text.slice(cursor - offset, end - offset));
    return html;
  }

  function snippet(text, spans, maxLen = 420) {
    if (!text) return { html: "", truncated: false };
    if (text.length <= maxLen) return { html: highlight(text, spans), truncated: false };
    let anchor = spans && spans.length ? spans[0][0] : 0;
    let start = Math.max(0, anchor - Math.floor(maxLen * 0.35));
    let end = Math.min(text.length, start + maxLen);
    if (end - start < maxLen) start = Math.max(0, end - maxLen);
    if (start > 0) { const ws = text.indexOf(" ", start); if (ws !== -1 && ws - start < 40) start = ws + 1; }
    if (end < text.length) { const ws = text.lastIndexOf(" ", end); if (ws !== -1 && end - ws < 40) end = ws; }
    const slice = text.slice(start, end);
    return {
      html: (start > 0 ? "… " : "") + highlight(slice, spans, start, slice.length) + (end < text.length ? " …" : ""),
      truncated: true,
    };
  }

  function formatTime(epoch) {
    const date = new Date(epoch * 1000);
    return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function relativeTime(epoch) {
    const diff = Math.max(0, Date.now() / 1000 - epoch);
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 30 * 86400) return `${Math.floor(diff / 86400)}d ago`;
    return formatTime(epoch);
  }

  function toast(message, isError = false) {
    const el = $("toast");
    el.textContent = message;
    el.className = "toast" + (isError ? " error" : "");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.add("hidden"), isError ? 6000 : 3000);
  }

  function showError(message) {
    const el = $("form-error");
    if (!message) { el.classList.add("hidden"); el.textContent = ""; return; }
    el.textContent = message;
    el.classList.remove("hidden");
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  }

  function localDateTimeValue(date) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  // ------------------------------------------------------------- form state
  function keywordSetFromForm() {
    return {
      id: state.currentSetId || "",
      name: $("kw-name").value.trim() || "Untitled",
      include_any: $("kw-include").value,
      require_any: $("kw-require").value,
      exclude: $("kw-exclude").value,
      exclude_authors: $("kw-authors").value.split(/[,\n]+/).map((s) => s.trim()).filter(Boolean),
      whole_word: $("kw-whole").checked,
      plural_tolerant: $("kw-plural").checked,
      case_sensitive: $("kw-case").checked,
    };
  }

  function fillKeywordForm(set) {
    state.currentSetId = set ? set.id : "";
    $("kw-select").value = state.currentSetId;
    $("kw-name").value = set ? set.name : "";
    $("kw-include").value = set ? (set.include_any || []).join("\n") : "";
    $("kw-require").value = set ? (set.require_any || []).join("\n") : "";
    $("kw-exclude").value = set ? (set.exclude || []).join("\n") : "";
    $("kw-authors").value = set ? (set.exclude_authors || []).join(", ") : "AutoModerator";
    $("kw-whole").checked = set ? set.whole_word !== false : true;
    $("kw-plural").checked = set ? set.plural_tolerant !== false : true;
    $("kw-case").checked = set ? !!set.case_sensitive : false;
  }

  function timeframeFromForm() {
    const tf = { preset: state.preset, tz: state.tz };
    if (state.preset === "custom") {
      tf.start = $("tf-start").value;
      tf.end = $("tf-end").value;
    }
    return tf;
  }

  function runParamsFromForm() {
    return {
      subreddits: $("subreddits").value,
      keyword_set: keywordSetFromForm(),
      timeframe: timeframeFromForm(),
      targets: { posts: $("target-posts").checked, comments: $("target-comments").checked },
      backend: $("backend").value || "arctic",
      max_pages: parseInt($("max-pages").value, 10) || 100,
    };
  }

  const saveForm = debounce(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        subreddits: $("subreddits").value,
        preset: state.preset,
        start: $("tf-start").value,
        end: $("tf-end").value,
        posts: $("target-posts").checked,
        comments: $("target-comments").checked,
        backend: $("backend").value,
        maxPages: $("max-pages").value,
        keywordDraft: keywordSetFromForm(),
      }));
    } catch (_) { /* storage may be unavailable */ }
  }, 300);

  function restoreForm() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null"); } catch (_) { saved = null; }
    const last = state.status.last_params;
    if (saved) {
      $("subreddits").value = saved.subreddits || "";
      state.preset = saved.preset || "day";
      $("tf-start").value = saved.start || "";
      $("tf-end").value = saved.end || "";
      $("target-posts").checked = saved.posts !== false;
      $("target-comments").checked = saved.comments !== false;
      if (saved.backend) $("backend").value = saved.backend;
      if (saved.maxPages) $("max-pages").value = saved.maxPages;
      if (saved.keywordDraft) {
        const draft = saved.keywordDraft;
        const known = state.keywordSets.find((s) => s.id === draft.id);
        fillKeywordForm({
          ...(known || {}), ...draft,
          include_any: String(draft.include_any || "").split("\n"),
          require_any: String(draft.require_any || "").split("\n"),
          exclude: String(draft.exclude || "").split("\n"),
          id: known ? known.id : "",
        });
        return;
      }
    } else if (last) {
      $("subreddits").value = (last.subreddits || []).join(", ");
      state.preset = last.timeframe?.preset || "day";
      $("target-posts").checked = last.targets?.posts !== false;
      $("target-comments").checked = last.targets?.comments !== false;
      if (last.backend) $("backend").value = last.backend;
    } else {
      $("subreddits").value = "smallbusiness, Entrepreneur, webdev, startups";
    }
    fillKeywordForm(state.keywordSets[0] || null);
  }

  // --------------------------------------------------------------- presets
  function renderPresets() {
    const box = $("presets");
    box.innerHTML = "";
    for (const preset of state.status.presets) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = preset.label;
      button.dataset.id = preset.id;
      button.className = preset.id === state.preset ? "active" : "";
      button.addEventListener("click", () => { state.preset = preset.id; renderPresets(); updateTimeframePreview(); saveForm(); });
      box.appendChild(button);
    }
    $("custom-range").classList.toggle("hidden", state.preset !== "custom");
    if (state.preset === "custom" && !$("tf-start").value) {
      const now = new Date();
      $("tf-end").value = localDateTimeValue(now);
      $("tf-start").value = localDateTimeValue(new Date(now.getTime() - 7 * 86400 * 1000));
    }
  }

  const updateTimeframePreview = debounce(async () => {
    const el = $("tf-preview");
    if (state.preset !== "custom") {
      el.textContent = `Relative to now · times shown in ${state.tz}`;
      return;
    }
    try {
      const preview = await api("/api/timeframe/preview", { method: "POST", body: { timeframe: timeframeFromForm() } });
      el.textContent = `Will search ${preview.start_local} → ${preview.end_local} (${preview.tz})`;
      el.classList.remove("error");
    } catch (error) {
      el.textContent = error.message;
    }
  }, 250);

  // ----------------------------------------------------------- keyword sets
  async function loadKeywordSets(selectId) {
    state.keywordSets = await api("/api/keyword-sets");
    const select = $("kw-select");
    select.innerHTML = '<option value="">— new keyword set —</option>';
    for (const set of state.keywordSets) {
      const option = document.createElement("option");
      option.value = set.id;
      option.textContent = set.name;
      select.appendChild(option);
    }
    if (selectId !== undefined) {
      const set = state.keywordSets.find((s) => s.id === selectId) || null;
      fillKeywordForm(set);
    } else {
      select.value = state.currentSetId;
    }
  }

  async function saveKeywordSet(asNew) {
    const body = keywordSetFromForm();
    if (asNew) body.id = "";
    if (!body.name || body.name === "Untitled") {
      const name = prompt("Name for this keyword set:", body.name === "Untitled" ? "" : body.name);
      if (name === null) return;
      body.name = name.trim() || "Untitled";
      $("kw-name").value = body.name;
    }
    try {
      const saved = await api("/api/keyword-sets", { method: "POST", body });
      await loadKeywordSets(saved.id);
      toast(`Saved “${saved.name}”`);
      saveForm();
    } catch (error) {
      showError(error.message);
    }
  }

  async function deleteKeywordSet() {
    if (!state.currentSetId) return;
    const set = state.keywordSets.find((s) => s.id === state.currentSetId);
    if (!confirm(`Delete keyword set “${set ? set.name : state.currentSetId}”?`)) return;
    await api(`/api/keyword-sets/${encodeURIComponent(state.currentSetId)}`, { method: "DELETE" });
    await loadKeywordSets(state.keywordSets.find((s) => s.id !== state.currentSetId)?.id || "");
    toast("Keyword set deleted");
  }

  async function testKeywords() {
    const text = $("kw-test-text").value;
    const out = $("kw-test-result");
    try {
      const result = await api("/api/keyword-sets/test", { method: "POST", body: { keyword_set: keywordSetFromForm(), title: "", text } });
      if (result.matched) {
        out.innerHTML = `<span class="kw-test-ok">✔ Matches</span> — ${result.matched_terms.map((t) => `<span class="chip">${escapeHtml(t)}</span>`).join(" ")}`;
      } else if (result.excluded_by.length) {
        out.innerHTML = `<span class="kw-test-no">✘ Excluded</span> by ${result.excluded_by.map((t) => `<span class="chip">${escapeHtml(t)}</span>`).join(" ")}`;
      } else if (result.missing_required) {
        out.innerHTML = `<span class="kw-test-no">✘ No match</span> — a search phrase hit, but none of the “must also contain” words did`;
      } else {
        out.innerHTML = `<span class="kw-test-no">✘ No match</span> — none of the search phrases appear`;
      }
      showError("");
    } catch (error) {
      out.textContent = "";
      showError(error.message);
    }
  }

  // ------------------------------------------------------- subreddit search
  async function searchSubreddits() {
    const query = $("sub-search").value.trim();
    const box = $("sub-results");
    if (!query) { box.classList.add("hidden"); return; }
    box.innerHTML = '<div class="sub-result muted">Searching…</div>';
    box.classList.remove("hidden");
    try {
      const data = await api(`/api/subreddits/search?q=${encodeURIComponent(query)}&backend=${encodeURIComponent($("backend").value || "arctic")}`);
      if (!data.results.length) { box.innerHTML = '<div class="sub-result muted">No subreddits found (name prefix search)</div>'; return; }
      box.innerHTML = "";
      for (const sub of data.results) {
        const row = document.createElement("div");
        row.className = "sub-result";
        row.innerHTML = `<div><b>r/${escapeHtml(sub.name)}</b> <span class="muted small">${sub.subscribers ? Number(sub.subscribers).toLocaleString() + " members" : ""}${sub.over_18 ? " · 18+" : ""}</span><div class="desc">${escapeHtml(sub.description || "")}</div></div><div class="muted small">＋ add</div>`;
        row.addEventListener("click", () => {
          const current = $("subreddits").value.split(/[\s,;]+/).filter(Boolean);
          if (!current.some((s) => s.toLowerCase().replace(/^r\//, "") === sub.name.toLowerCase())) {
            current.push(sub.name);
            $("subreddits").value = current.join(", ");
            saveForm();
          }
          toast(`Added r/${sub.name}`);
        });
        box.appendChild(row);
      }
    } catch (error) {
      box.innerHTML = `<div class="sub-result muted">${escapeHtml(error.message)}</div>`;
    }
  }

  // ------------------------------------------------------------------- runs
  async function startRun() {
    showError("");
    const params = runParamsFromForm();
    $("btn-run").disabled = true;
    try {
      const data = await api("/api/runs", { method: "POST", body: params });
      state.runId = data.run_id;
      state.results = [];
      state.resultsVersion = -1;
      state.shown = PAGE_SIZE;
      renderResults();
      renderProgress(data.job, null);
      startPolling();
      $("progress").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      showError(error.message);
      $("btn-run").disabled = false;
    }
  }

  async function cancelRun() {
    if (!state.runId) return;
    try { await api(`/api/runs/${state.runId}/cancel`, { method: "POST" }); toast("Cancelling…"); }
    catch (error) { toast(error.message, true); }
  }

  function startPolling() {
    stopPolling();
    poll();
    state.pollTimer = setInterval(poll, 1000);
  }

  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  async function poll() {
    if (!state.runId) return;
    try {
      const run = await api(`/api/runs/${state.runId}`);
      state.run = run;
      state.job = run.job;
      renderProgress(run.job, run);
      const terminal = ["done", "failed", "cancelled"].includes(run.job ? run.job.status : run.status);
      const version = run.job ? run.job.results_version : 0;
      if (version !== state.resultsVersion || terminal) {
        state.resultsVersion = version;
        await loadResults();
      }
      if (terminal) {
        stopPolling();
        $("btn-run").disabled = false;
        $("btn-cancel").classList.add("hidden");
        $("btn-run").classList.remove("hidden");
        await loadResults();
      } else {
        $("btn-cancel").classList.remove("hidden");
        $("btn-run").classList.add("hidden");
      }
    } catch (error) {
      toast(error.message, true);
      stopPolling();
      $("btn-run").disabled = false;
    }
  }

  async function loadResults() {
    if (!state.runId) return;
    const data = await api(`/api/runs/${state.runId}/results`);
    state.results = data.results;
    state.run = data.run;
    if (data.job) state.job = data.job;
    renderResults();
  }

  function renderProgress(job, run) {
    const box = $("progress");
    box.classList.remove("hidden");
    const status = job ? job.status : (run ? run.status : "queued");
    const dot = $("progress-status");
    dot.className = "status-dot " + status;
    const phase = job ? job.phase : (run ? `Run #${run.id} (${run.status})` : "Starting…");
    $("progress-phase").textContent = status === "running" ? phase : `${phase} — ${status}`;
    $("progress-line").textContent = job ? job.progress_line : "";
    const stats = run && run.stats ? run.stats : null;
    $("c-fetched").textContent = job ? job.fetched.toLocaleString() : (stats ? (stats.fetched_posts + stats.fetched_comments).toLocaleString() : "0");
    $("c-matched").textContent = job ? job.matched.toLocaleString() : (stats ? (stats.matched_posts + stats.matched_comments).toLocaleString() : "0");
    $("c-new").textContent = job ? job.new_items.toLocaleString() : (stats ? stats.new_items.toLocaleString() : "0");
    $("c-requests").textContent = job ? job.requests.toLocaleString() : (stats ? String(stats.requests) : "0");
    const elapsed = job ? job.elapsed : (run && run.finished_at ? run.finished_at - run.created_at : 0);
    $("c-elapsed").textContent = elapsed >= 60 ? `${Math.floor(elapsed / 60)}m ${Math.round(elapsed % 60)}s` : `${Math.round(elapsed)}s`;
    const bar = $("progress-bar");
    if (status === "running") {
      if (job && job.steps > 0) { bar.className = "bar-fill"; bar.style.width = `${Math.max(3, Math.round((job.step / job.steps) * 100))}%`; }
      else { bar.className = "bar-fill indeterminate"; bar.style.width = ""; }
    } else {
      bar.className = "bar-fill";
      bar.style.width = status === "done" ? "100%" : bar.style.width || "0%";
    }
    const warnings = (job && job.warnings.length ? job.warnings : (run ? run.warnings : [])) || [];
    const warnBox = $("progress-warnings");
    if (warnings.length) {
      warnBox.innerHTML = `<b>Partial coverage</b><ul>${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;
      warnBox.classList.remove("hidden");
    } else {
      warnBox.classList.add("hidden");
    }
    const error = (job && job.error) || (run && run.error);
    const errBox = $("progress-error");
    if (error) { errBox.textContent = error; errBox.classList.remove("hidden"); } else { errBox.classList.add("hidden"); }
    if (job) $("progress-log").textContent = job.log.join("\n");
    else if (run) $("progress-log").textContent = `Run #${run.id} · ${run.status}\n` + (run.warnings || []).join("\n");
  }

  // ---------------------------------------------------------------- results
  function filteredResults() {
    const text = $("filter-text").value.trim().toLowerCase();
    const kind = $("filter-kind").value;
    const sub = $("filter-sub").value;
    const onlyNew = $("filter-new").checked;
    let rows = state.results.filter((r) =>
      (!kind || r.kind === kind) &&
      (!sub || r.subreddit.toLowerCase() === sub.toLowerCase()) &&
      (!onlyNew || r.is_new) &&
      (!text || (r.title + " " + r.text + " " + r.author + " " + r.matched.join(" ")).toLowerCase().includes(text)));
    const sort = $("sort").value;
    const by = {
      newest: (a, b) => b.created_utc - a.created_utc,
      oldest: (a, b) => a.created_utc - b.created_utc,
      relevance: (a, b) => (b.relevance - a.relevance) || (b.created_utc - a.created_utc),
      score: (a, b) => ((b.score ?? -1) - (a.score ?? -1)) || (b.created_utc - a.created_utc),
      comments: (a, b) => ((b.num_comments ?? -1) - (a.num_comments ?? -1)) || (b.created_utc - a.created_utc),
    }[sort] || ((a, b) => b.created_utc - a.created_utc);
    return rows.sort(by);
  }

  function renderSubredditFilter() {
    const select = $("filter-sub");
    const current = select.value;
    const subs = [...new Set(state.results.map((r) => r.subreddit))].sort((a, b) => a.localeCompare(b));
    select.innerHTML = '<option value="">All subreddits</option>' + subs.map((s) => `<option value="${escapeHtml(s)}">r/${escapeHtml(s)}</option>`).join("");
    if (subs.includes(current)) select.value = current;
  }

  function renderResults() {
    renderSubredditFilter();
    const list = $("results-list");
    const rows = filteredResults();
    const total = state.results.length;
    const heading = $("results-heading");
    heading.textContent = state.run ? `Results · run #${state.run.id}` : "Results";
    $("results-count").textContent = total ? `${rows.length.toLocaleString()} of ${total.toLocaleString()} shown · ${state.results.filter((r) => r.is_new).length.toLocaleString()} new` : "";
    $("btn-export-csv").disabled = !total;
    $("btn-export-json").disabled = !total;
    if (!total) {
      const running = state.job && state.job.status === "running";
      list.innerHTML = `<div class="empty"><div class="empty-icon">${running ? "⏳" : "🗂️"}</div><p>${running ? "Searching… matches will appear here as each subreddit finishes." : (state.run ? "No matches in this run." : "Pick subreddits, keywords and a timeframe, then press <b>Search Reddit</b>.")}</p></div>`;
      return;
    }
    if (!rows.length) {
      list.innerHTML = '<div class="empty"><p>No results match the current filters.</p></div>';
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const row of rows.slice(0, state.shown)) fragment.appendChild(renderResult(row));
    list.innerHTML = "";
    list.appendChild(fragment);
    if (rows.length > state.shown) {
      const more = document.createElement("button");
      more.className = "btn ghost";
      more.textContent = `Show ${Math.min(PAGE_SIZE, rows.length - state.shown)} more (${rows.length - state.shown} hidden)`;
      more.addEventListener("click", () => { state.shown += PAGE_SIZE; renderResults(); });
      list.appendChild(more);
    }
  }

  function renderResult(row) {
    const el = document.createElement("article");
    el.className = "result";
    const isPost = row.kind === "post";
    const titleHtml = isPost
      ? `<div class="result-title"><a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">${highlight(row.title, row.title_spans) || "(no title)"}</a></div>`
      : (row.title ? `<div class="result-context">💬 comment on <a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">${escapeHtml(row.title)}</a></div>` : "");
    const snip = snippet(row.text, row.text_spans);
    const stats = [];
    if (row.score !== null && row.score !== undefined) stats.push(`▲ ${row.score}`);
    if (isPost && row.num_comments !== null && row.num_comments !== undefined) stats.push(`💬 ${row.num_comments}`);
    el.innerHTML = `
      <div class="result-meta">
        <span class="badge ${row.kind}">${row.kind}</span>
        ${row.is_new ? '<span class="badge new">new</span>' : ""}
        <span class="sub">r/${escapeHtml(row.subreddit)}</span>
        <span>u/${escapeHtml(row.author)}</span>
        <span title="${escapeHtml(formatTime(row.created_utc))}">${relativeTime(row.created_utc)}</span>
        ${row.flair ? `<span class="chip">${escapeHtml(row.flair)}</span>` : ""}
        <span class="stats">${stats.join(" ")}</span>
      </div>
      ${titleHtml}
      <div class="result-text">${snip.html}</div>
      <div class="result-foot">
        ${row.matched.map((t) => `<span class="chip" title="matched keyword">${escapeHtml(t)}</span>`).join("")}
        <span class="spacer"></span>
        ${snip.truncated ? '<button class="linkbtn" data-action="expand">Show full text</button>' : ""}
        <a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">Open on Reddit ↗</a>
      </div>`;
    const expand = el.querySelector('[data-action="expand"]');
    if (expand) {
      expand.addEventListener("click", () => {
        const textEl = el.querySelector(".result-text");
        if (expand.dataset.open) {
          textEl.innerHTML = snip.html; expand.textContent = "Show full text"; delete expand.dataset.open;
        } else {
          textEl.innerHTML = highlight(row.text, row.text_spans); expand.textContent = "Show less"; expand.dataset.open = "1";
        }
      });
    }
    return el;
  }

  // ---------------------------------------------------------------- history
  async function openHistory() {
    const list = $("history-list");
    list.innerHTML = '<div class="muted">Loading…</div>';
    $("modal-history").classList.remove("hidden");
    const runs = await api("/api/runs");
    if (!runs.length) { list.innerHTML = '<div class="muted">No searches yet.</div>'; return; }
    list.innerHTML = "";
    for (const run of runs) {
      const row = document.createElement("div");
      row.className = "history-row";
      const p = run.params || {};
      const tfLabel = p.timeframe_resolved ? p.timeframe_resolved.label : (p.timeframe ? p.timeframe.preset : "");
      row.innerHTML = `
        <div>
          <div class="h-main">#${run.id} · ${escapeHtml((p.subreddits || []).map((s) => "r/" + s).join(", "))}</div>
          <div class="h-sub">${escapeHtml(formatTime(run.created_at))} · ${escapeHtml(tfLabel)} · “${escapeHtml(p.keyword_set ? p.keyword_set.name : "")}” · ${escapeHtml(run.status)} · ${run.result_count} result${run.result_count === 1 ? "" : "s"}${run.stats ? ` (${run.stats.new_items} new)` : ""}</div>
        </div>
        <div class="h-actions">
          <button class="btn small" data-action="load">Load</button>
          <button class="btn small ghost" data-action="reuse" title="Copy this run's subreddits, keywords and timeframe into the form">Reuse setup</button>
          <button class="btn small ghost" data-action="delete">Delete</button>
        </div>`;
      row.querySelector('[data-action="load"]').addEventListener("click", async () => {
        state.runId = run.id;
        state.resultsVersion = -1;
        state.shown = PAGE_SIZE;
        $("modal-history").classList.add("hidden");
        await poll();
        if (!["done", "failed", "cancelled"].includes(run.status)) startPolling();
      });
      row.querySelector('[data-action="reuse"]').addEventListener("click", () => {
        $("subreddits").value = (p.subreddits || []).join(", ");
        if (p.keyword_set) fillKeywordForm({ ...p.keyword_set, id: state.keywordSets.some((s) => s.id === p.keyword_set.id) ? p.keyword_set.id : "" });
        if (p.timeframe) {
          state.preset = p.timeframe.preset || "day";
          if (p.timeframe.start) $("tf-start").value = p.timeframe.start;
          if (p.timeframe.end) $("tf-end").value = p.timeframe.end;
          renderPresets(); updateTimeframePreview();
        }
        if (p.targets) { $("target-posts").checked = !!p.targets.posts; $("target-comments").checked = !!p.targets.comments; }
        if (p.backend) $("backend").value = p.backend;
        $("modal-history").classList.add("hidden");
        saveForm();
        toast("Setup copied into the form");
      });
      row.querySelector('[data-action="delete"]').addEventListener("click", async () => {
        if (!confirm(`Delete run #${run.id} and its results?`)) return;
        try {
          await api(`/api/runs/${run.id}`, { method: "DELETE" });
          if (state.runId === run.id) { state.runId = null; state.results = []; state.run = null; state.job = null; renderResults(); $("progress").classList.add("hidden"); }
          openHistory();
        } catch (error) { toast(error.message, true); }
      });
      list.appendChild(row);
    }
  }

  // --------------------------------------------------------------- settings
  async function openSettings() {
    $("modal-settings").classList.remove("hidden");
    $("cred-result").textContent = "";
    const creds = await api("/api/settings/credentials");
    renderCredentials(creds);
  }

  function renderCredentials(creds) {
    $("cred-id").value = creds.client_id || "";
    $("cred-secret").value = "";
    $("cred-secret").placeholder = creds.has_secret ? "•••••••• (saved — leave blank to keep)" : "";
    $("cred-user").value = creds.username || "";
    $("cred-ua").value = creds.user_agent || "";
    $("cred-status").textContent = creds.configured ? "✔ Credentials are configured." : "Not configured — the official Reddit API is unavailable; Arctic Shift still works.";
    updateBackendPill();
  }

  async function saveCredentials() {
    const body = {
      client_id: $("cred-id").value.trim(),
      client_secret: $("cred-secret").value ? $("cred-secret").value : null,
      user_agent: $("cred-ua").value.trim(),
      username: $("cred-user").value.trim(),
    };
    try {
      const creds = await api("/api/settings/credentials", { method: "PUT", body });
      await refreshStatus();
      renderCredentials(creds);
      $("cred-result").textContent = "Saved.";
    } catch (error) { $("cred-result").textContent = error.message; }
  }

  async function testCredentials() {
    $("cred-result").textContent = "Testing…";
    try {
      const result = await api("/api/settings/credentials/test", { method: "POST" });
      const limits = result.rate_limit && result.rate_limit.remaining !== undefined ? ` · rate limit remaining: ${result.rate_limit.remaining}` : "";
      $("cred-result").textContent = `✔ ${result.sample}${limits}`;
    } catch (error) { $("cred-result").textContent = `✘ ${error.message}`; }
  }

  async function clearCredentials() {
    if (!confirm("Remove the stored Reddit API credentials?")) return;
    const creds = await api("/api/settings/credentials", { method: "DELETE" });
    await refreshStatus();
    renderCredentials(creds);
    $("cred-result").textContent = "Removed.";
  }

  async function purgeData() {
    if (!confirm("Delete ALL runs and results? Saved keyword sets are kept.")) return;
    try {
      await api("/api/data/purge", { method: "POST" });
      state.runId = null; state.results = []; state.run = null; state.job = null;
      renderResults(); $("progress").classList.add("hidden");
      toast("All runs deleted");
    } catch (error) { toast(error.message, true); }
  }

  // ----------------------------------------------------------------- status
  async function refreshStatus() {
    state.status = await api("/api/status");
    const select = $("backend");
    const current = select.value;
    select.innerHTML = "";
    for (const backend of state.status.backends) {
      const option = document.createElement("option");
      option.value = backend.id;
      option.textContent = backend.label + (backend.available ? "" : " — not configured");
      option.disabled = !backend.available;
      select.appendChild(option);
    }
    select.value = current && [...select.options].some((o) => o.value === current && !o.disabled) ? current : "arctic";
    updateBackendPill();
  }

  function updateBackendPill() {
    const pill = $("backend-pill");
    const creds = state.status ? state.status.credentials : null;
    pill.textContent = creds && creds.configured ? "Arctic Shift + Reddit API" : "Arctic Shift (no login needed)";
    pill.className = "pill ok";
  }

  // ------------------------------------------------------------------- init
  function bind() {
    $("btn-run").addEventListener("click", startRun);
    $("btn-cancel").addEventListener("click", cancelRun);
    $("btn-sub-search").addEventListener("click", searchSubreddits);
    $("sub-search").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); searchSubreddits(); } });
    $("kw-select").addEventListener("change", (e) => {
      const set = state.keywordSets.find((s) => s.id === e.target.value) || null;
      fillKeywordForm(set);
      saveForm();
    });
    $("btn-kw-save").addEventListener("click", () => saveKeywordSet(false));
    $("btn-kw-saveas").addEventListener("click", () => saveKeywordSet(true));
    $("btn-kw-delete").addEventListener("click", deleteKeywordSet);
    $("btn-kw-test-toggle").addEventListener("click", () => $("kw-test").classList.toggle("hidden"));
    $("btn-kw-test").addEventListener("click", testKeywords);
    for (const id of ["tf-start", "tf-end"]) $(id).addEventListener("change", () => { updateTimeframePreview(); saveForm(); });
    for (const id of ["subreddits", "kw-name", "kw-include", "kw-require", "kw-exclude", "kw-authors", "max-pages"]) $(id).addEventListener("input", saveForm);
    for (const id of ["kw-whole", "kw-plural", "kw-case", "target-posts", "target-comments", "backend"]) $(id).addEventListener("change", saveForm);
    for (const id of ["filter-text", "filter-kind", "filter-sub", "sort", "filter-new"]) $(id).addEventListener("input", () => { state.shown = PAGE_SIZE; renderResults(); });
    $("btn-export-csv").addEventListener("click", () => { if (state.runId) window.location.href = `/api/runs/${state.runId}/export?format=csv`; });
    $("btn-export-json").addEventListener("click", () => { if (state.runId) window.location.href = `/api/runs/${state.runId}/export?format=json`; });
    $("btn-history").addEventListener("click", openHistory);
    $("btn-settings").addEventListener("click", openSettings);
    $("btn-help").addEventListener("click", () => $("modal-help").classList.remove("hidden"));
    $("btn-cred-save").addEventListener("click", saveCredentials);
    $("btn-cred-test").addEventListener("click", testCredentials);
    $("btn-cred-clear").addEventListener("click", clearCredentials);
    $("btn-purge").addEventListener("click", purgeData);
    for (const modal of document.querySelectorAll(".modal")) {
      modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
      modal.querySelector(".modal-close").addEventListener("click", () => modal.classList.add("hidden"));
    }
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".modal").forEach((m) => m.classList.add("hidden")); });
  }

  async function init() {
    $("tz-name").textContent = state.tz;
    bind();
    try {
      await refreshStatus();
      await loadKeywordSets();
      restoreForm();
      renderPresets();
      updateTimeframePreview();
      if (state.status.current_job) {
        state.runId = state.status.current_job.id;
        startPolling();
      } else if (state.status.last_params) {
        // show the most recent run's results on startup
        const runs = await api("/api/runs?limit=1");
        if (runs.length) { state.runId = runs[0].id; await poll(); }
      }
    } catch (error) {
      showError(`Could not talk to the local server: ${error.message}`);
    }
  }

  init();
})();
