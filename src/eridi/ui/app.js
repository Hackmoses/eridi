// Eridi's local web UI. Plain JavaScript, no build step.
// Every piece of document text is inserted as a text node, never as HTML.
"use strict";

const OUTCOMES = ["worked", "partial", "failed", "unknown"];
const main = document.getElementById("main");

// --- helpers ---

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const init = { credentials: "same-origin", headers: {}, ...options };
  if (options.body !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch("/api" + path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.error || (data.detail && JSON.stringify(data.detail)) || response.statusText;
    throw new Error(message);
  }
  return data;
}

function when(timestamp) {
  if (!timestamp) return "";
  const d = new Date(timestamp);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function historyLink(fileId, label) {
  return el("a", { href: "#/history?file=" + encodeURIComponent(fileId) }, label);
}

function ago(timestamp) {
  const seconds = (Date.now() - new Date(timestamp).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return Math.floor(seconds / 60) + " min ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + " h ago";
  if (seconds < 2 * 86400) return "yesterday";
  if (seconds < 14 * 86400) return Math.floor(seconds / 86400) + " days ago";
  return new Date(timestamp).toLocaleDateString(undefined, { dateStyle: "medium" });
}

function historyBadge(r) {
  const h = r.history;
  if (!h || (h.versions < 2 && !h.notes)) return null;
  const parts = [];
  if (h.last_edited) parts.push("edited " + ago(h.last_edited));
  if (h.versions > 1) parts.push(h.versions + " versions");
  if (h.notes) parts.push(h.notes + (h.notes === 1 ? " note" : " notes"));
  return el("div", { class: "story" },
    historyLink(r.file_id, parts.join(" · ")),
    h.needs_why ? [" · ", el("a", { href: "#/changes", class: "needs-why" }, "needs a why")] : null);
}

function snippet(parts) {
  return el("p", { class: "snippet" },
    (parts || []).map(([text, hit]) => (hit ? el("mark", {}, text) : text)));
}

function showError(target, error) {
  target.replaceChildren(el("p", { class: "error", role: "alert" }, error.message || String(error)));
}

function outcomeSelect(id) {
  return el("select", { id, name: "outcome" },
    el("option", { value: "" }, "Outcome"),
    OUTCOMES.map((o) => el("option", { value: o }, o)));
}

function diffView(diff) {
  if (!diff.available) return el("p", { class: "muted" }, "Earlier text not stored.");
  return el("pre", { class: "diff" },
    diff.lines.map((line) => {
      if (line.op === "@") return el("span", { class: "gap" }, "…\n");
      const cls = line.op === "+" ? "add" : line.op === "-" ? "del" : "ctx";
      return el("span", { class: cls }, `${line.op} ${line.text}\n`);
    }));
}

function entryCard(entry, currentFile) {
  const files = entry.files.filter((f) => f.id !== currentFile);
  const meta = [entry.kind, entry.author + (entry.author_kind === "agent" ? " (AI)" : ""), when(entry.created_at)];
  return el("article", { class: "entry state-" + entry.state },
    el("div", { class: "meta" },
      meta.join(" · "),
      entry.outcome ? el("span", { class: "outcome outcome-" + entry.outcome }, entry.outcome) : null,
      entry.state !== "current" ? el("span", { class: "state" }, entry.state) : null),
    entry.change ? el("p", { class: "change" }, entry.change) : null,
    entry.text ? el("p", { class: "text" }, entry.text) : null,
    files.length ? el("p", { class: "files" }, files.map((f) => historyLink(f.id, f.path))) : null);
}

// --- screens ---

let searchTimer = null;

function renderSearch(params) {
  const query = params.get("q") || "";
  const input = el("input", {
    type: "search", id: "q", name: "q", value: query, autocomplete: "off",
    "aria-label": "Search files and notes", placeholder: "Search files and notes",
  });
  const results = el("div", { id: "results", "aria-live": "polite" });
  const form = el("form", { class: "searchbox", role: "search", onsubmit: (e) => { e.preventDefault(); run(); } },
    input, el("button", { type: "submit" }, "Search"));
  main.replaceChildren(form, results);
  input.focus();

  async function run() {
    const q = input.value.trim();
    history.replaceState(null, "", "#/search" + (q ? "?q=" + encodeURIComponent(q) : ""));
    if (!q) { results.replaceChildren(); return; }
    try {
      const data = await api("/search?q=" + encodeURIComponent(q));
      if (!data.results.length) {
        results.replaceChildren(el("p", { class: "muted" }, "Nothing found."));
        return;
      }
      const grouped = data.results.some((r) => r.match !== "all");
      let group = null;
      results.replaceChildren(el("ul", { class: "results" }, data.results.flatMap((r) => {
        const heading = grouped && r.group !== group ? el("li", { class: "group" }, r.group) : null;
        group = r.group;
        const isFile = r.type === "file";
        const title = isFile && r.file_id ? historyLink(r.file_id, r.title) : el("span", {}, r.title);
        const meta = isFile
          ? [r.storage, r.path].filter(Boolean).join(" · ")
          : [r.kind, r.author, when(r.created_at), r.path].filter(Boolean).join(" · ");
        const item = el("li", { class: "result " + r.type },
          el("div", { class: "title" }, el("span", { class: "tag" }, isFile ? "File" : "Note"), title),
          el("div", { class: "meta" }, meta, !isFile && r.file_id ? [" · ", historyLink(r.file_id, "history")] : null),
          isFile ? historyBadge(r) : null,
          snippet(r.snippet));
        return heading ? [heading, item] : [item];
      })));
    } catch (error) { showError(results, error); }
  }

  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(run, 250);
  });
  if (query) run();
}

function dayLabel(timestamp) {
  const d = new Date(timestamp);
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86400000);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

async function renderActivity() {
  const list = el("div", { class: "activity" });
  main.replaceChildren(el("h1", {}, "Activity"), list);
  try {
    const data = await api("/activity?limit=100");
    if (!data.activity.length) {
      list.append(el("p", { class: "muted" }, "No edits yet. Changes appear here seconds after you save a file."));
      return;
    }
    let day = null;
    for (const a of data.activity) {
      const label = dayLabel(a.at);
      if (label !== day) {
        day = label;
        list.append(el("h2", { class: "day" }, label));
      }
      list.append(activityItem(a));
    }
  } catch (error) { showError(list, error); }
}

function activityItem(a) {
  const time = new Date(a.at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const diffBox = el("div", { class: "diffbox", hidden: true });
  const toggle = a.kind === "edited" ? el("button", { type: "button", class: "link", "aria-expanded": "false",
    onclick: async () => {
      const open = diffBox.hidden;
      diffBox.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (open && !diffBox.childElementCount) {
        const query = `/diff?file=${encodeURIComponent(a.file_id)}&from=${a.from}&to=${a.to}`;
        try { diffBox.append(diffView(await api(query))); } catch (error) { showError(diffBox, error); }
      }
    } }, "Diff") : null;
  return el("article", { class: "event" },
    el("div", { class: "meta" }, time, " · ", a.kind === "edited" ? "Edited" : "Added",
      a.storage ? " · " + a.storage : "", a.status !== "present" ? " · " + a.status : ""),
    el("h3", {}, historyLink(a.file_id, a.path)),
    a.summary ? el("p", { class: "summary" }, a.summary) : null,
    a.notes.map((n) => el("p", { class: "why" }, n.text,
      el("span", { class: "muted" }, " — " + n.author), n.outcome ? el("span", { class: "outcome outcome-" + n.outcome }, n.outcome) : null)),
    a.needs_why ? el("p", {}, el("a", { href: "#/changes" }, "Needs a why")) : null,
    toggle, diffBox);
}

async function renderChanges() {
  main.replaceChildren(el("h1", {}, "Changes"));
  const list = el("div", { class: "changes" });
  main.append(list);
  try {
    const data = await api("/changes");
    if (!data.changes.length) {
      list.append(el("p", { class: "muted" }, "Nothing waiting for a why."));
      return;
    }
    data.changes.forEach((change, i) => list.append(changeCard(change, i)));
  } catch (error) { showError(list, error); }
}

function changeCard(change, i) {
  const why = el("textarea", { id: "why-" + i, name: "why", rows: 3, required: true });
  const outcome = outcomeSelect("outcome-" + i);
  const message = el("p", { class: "error", role: "alert", hidden: true });
  const diffBox = el("div", { class: "diffbox", hidden: true });
  const toggle = el("button", { type: "button", class: "link", "aria-expanded": "false",
    onclick: async () => {
      const open = diffBox.hidden;
      diffBox.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (open && !diffBox.childElementCount) {
        try { diffBox.append(diffView(await api("/diff?file=" + encodeURIComponent(change.file_id)))); }
        catch (error) { showError(diffBox, error); }
      }
    } }, "Diff");
  const card = el("form", { class: "card", "aria-labelledby": "change-" + i,
    onsubmit: async (e) => {
      e.preventDefault();
      if (!why.value.trim()) { why.focus(); return; }
      try {
        await api("/notes", { body: { text: why.value, file: change.file_id, outcome: outcome.value || null } });
        card.replaceWith(el("p", { class: "saved", role: "status" }, "Saved: " + change.path));
        refreshStatus();
      } catch (error) { message.hidden = false; message.textContent = error.message; }
    } },
    el("h2", { id: "change-" + i }, historyLink(change.file_id, change.path)),
    el("div", { class: "meta" }, [change.storage, when(change.detected_at)].filter(Boolean).join(" · ")),
    el("p", { class: "summary" }, change.summary || "Text changed"),
    toggle, diffBox,
    el("label", { for: "why-" + i }, "Why"), why,
    el("div", { class: "actions" },
      el("label", { for: "outcome-" + i, class: "visually-hidden" }, "Outcome"), outcome,
      el("button", { type: "submit" }, "Save"),
      el("button", { type: "button", class: "secondary", onclick: async () => {
        try {
          await api("/changes/dismiss", { body: { key: change.key } });
          card.remove();
          refreshStatus();
        } catch (error) { message.hidden = false; message.textContent = error.message; }
      } }, "Dismiss")),
    message);
  return card;
}

async function renderHistory(params) {
  const file = params.get("file");
  main.replaceChildren();
  if (!file) { main.append(el("p", { class: "muted" }, "Choose a file from search or changes.")); return; }
  try {
    const data = await api("/history?file=" + encodeURIComponent(file));
    const f = data.file;
    const events = [
      ...data.versions.map((v) => ({ at: v.first_seen, node: el("li", { class: "version" },
        el("div", { class: "meta" }, when(v.first_seen)),
        el("p", {}, v.current ? "Current version" : "Version", el("code", {}, " " + v.text_sha256.slice(0, 8)),
          v.available ? null : el("span", { class: "muted" }, " (text not stored)"))) })),
      ...data.moves.map((m) => ({ at: m.at, node: el("li", { class: "move" },
        el("div", { class: "meta" }, when(m.at)), el("p", {}, "Moved from " + m.from)) })),
      ...data.entries.map((e) => ({ at: e.created_at, node: el("li", {}, entryCard(e, f.id)) })),
    ].sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
    const text = el("textarea", { id: "note", name: "note", rows: 3, required: true });
    const outcome = outcomeSelect("note-outcome");
    const message = el("p", { class: "error", role: "alert", hidden: true });
    const form = el("form", { class: "card", onsubmit: async (e) => {
      e.preventDefault();
      if (!text.value.trim()) { text.focus(); return; }
      try {
        await api("/notes", { body: { text: text.value, file: f.id, outcome: outcome.value || null } });
        renderHistory(params);
        refreshStatus();
      } catch (error) { message.hidden = false; message.textContent = error.message; }
    } },
      el("label", { for: "note" }, "Add a note"), text,
      el("div", { class: "actions" },
        el("label", { for: "note-outcome", class: "visually-hidden" }, "Outcome"), outcome,
        el("button", { type: "submit" }, "Save")),
      message);
    main.append(
      el("h1", {}, f.path),
      el("div", { class: "meta" }, [f.storage, f.title, f.status !== "present" ? f.status : null].filter(Boolean).join(" · ")),
      el("ol", { class: "timeline" }, events.map((e) => e.node)),
      form);
  } catch (error) { showError(main, error); }
}

async function renderStorage() {
  main.replaceChildren(el("h1", {}, "Storage"));
  const list = el("div", {});
  main.append(list);
  try {
    const data = await api("/storage");
    if (!data.storage.length) {
      list.append(el("p", { class: "muted" }, "No storage yet. In a terminal: eridi storage add <folder>"));
      return;
    }
    list.append(el("ul", { class: "storage" }, data.storage.map((root, i) => {
      const toggle = (field, label) => {
        const id = `${field}-${i}`;
        const box = el("input", { type: "checkbox", id, checked: root[field], onchange: async () => {
          try { Object.assign(root, await api("/storage/" + encodeURIComponent(root.id), { method: "PATCH", body: { [field]: box.checked } })); }
          catch (error) { box.checked = !box.checked; alert(error.message); }
        } });
        return el("label", { for: id, class: "switch" }, box, " " + label);
      };
      return el("li", { class: "card" },
        el("h2", {}, root.label),
        el("div", { class: "meta" }, root.location),
        el("div", { class: "actions" }, toggle("search", "Search"), toggle("save", "Save")));
    })));
  } catch (error) { showError(list, error); }
}

// --- status and scanning ---

const scanButton = document.getElementById("scan-now");
const scanStatus = document.getElementById("scan-status");
const pendingBadge = document.getElementById("pending-count");
let polling = null;

let lastActivity;

async function refreshStatus() {
  try {
    const s = await api("/status");
    if (lastActivity !== undefined && s.last_activity !== lastActivity) {
      // Something was just saved or scanned: refresh the lists that show it.
      if (location.hash.startsWith("#/activity")) renderActivity();
      else if (location.hash.startsWith("#/changes")) renderChanges();
    }
    lastActivity = s.last_activity;
    if (s.pending_count !== null) {
      pendingBadge.hidden = !s.pending_count;
      pendingBadge.textContent = s.pending_count || "";
    }
    scanButton.disabled = s.scan.running || !s.storage_count;
    scanStatus.textContent = s.scan.running ? "Scanning…"
      : s.scan.last_error ? "Scan failed: " + s.scan.last_error
      : s.scan.last_finished ? "Scanned " + when(s.scan.last_finished) : "";
    if (!s.initialized && !document.getElementById("setup")) {
      main.prepend(el("p", { id: "setup", class: "notice" }, "Set up first: run eridi init in a terminal."));
    }
    return s;
  } catch (error) {
    scanStatus.textContent = error.message;
    return null;
  }
}

scanButton.addEventListener("click", async () => {
  try {
    await api("/scan", { method: "POST" });
  } catch (error) { scanStatus.textContent = error.message; return; }
  clearInterval(polling);
  scanButton.disabled = true;
  scanStatus.textContent = "Scanning…";
  polling = setInterval(async () => {
    const s = await refreshStatus();
    if (s && !s.scan.running) {
      clearInterval(polling);
      if (location.hash.startsWith("#/changes")) renderChanges();
    }
  }, 1000);
});

// --- routing ---

function route() {
  const hash = location.hash || "#/search";
  const [path, query] = hash.slice(1).split("?");
  const params = new URLSearchParams(query || "");
  const name = path.replace(/^\//, "") || "search";
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (name === "changes") renderChanges();
  else if (name === "activity") renderActivity();
  else if (name === "history") renderHistory(params);
  else if (name === "storage") renderStorage();
  else renderSearch(params);
}

window.addEventListener("hashchange", route);
route();
refreshStatus();
setInterval(refreshStatus, 5000);
