// Eridi's local web UI. Plain JavaScript, no build step.
// Every piece of document text is inserted as a text node, never as HTML.
"use strict";

const OUTCOMES = ["worked", "partial", "failed", "unknown"];
const PATIENT_DATA = "patient-data";
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

function outcomeBadge(result) {
  return result ? el("span", { class: "outcome outcome-" + result }, result) : null;
}

// Three buttons that record "did it work?" for an entry or a file version.
function quickOutcome(body, done, label) {
  const box = el("div", { class: "quick-outcome", role: "group", "aria-label": label || "Did it work?" });
  for (const [result, text] of [["worked", "Worked"], ["partial", "Partly"], ["failed", "Failed"]]) {
    box.append(el("button", { type: "button", class: "secondary small", onclick: async () => {
      try { await api("/outcomes", { body: { ...body, outcome: result } }); done(result); }
      catch (error) { alert(error.message); }
    } }, text));
  }
  return box;
}

function entryCard(entry, currentFile, onOutcome) {
  const files = entry.files.filter((f) => f.id !== currentFile);
  const meta = [entry.kind, entry.author + (entry.author_kind === "agent" ? " (AI)" : ""), when(entry.created_at)];
  const result = entry.result || entry.outcome;
  const askable = onOutcome && entry.state === "current" && (entry.kind === "note" || entry.kind === "decision");
  return el("article", { class: "entry state-" + entry.state },
    el("div", { class: "meta" },
      meta.join(" · "),
      outcomeBadge(result),
      entry.state !== "current" ? el("span", { class: "state" }, entry.state) : null),
    entry.change ? el("p", { class: "change" }, entry.change) : null,
    entry.text ? el("p", { class: "text" }, entry.text) : null,
    files.length ? el("p", { class: "files" }, files.map((f) => historyLink(f.id, f.path))) : null,
    askable ? quickOutcome({ entry_id: entry.id }, onOutcome) : null);
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
    quickOutcome({ file: change.file_id }, (result) => {
      const badge = card.querySelector(".summary");
      badge.replaceChildren(change.summary || "Text changed", " ", outcomeBadge(result));
    }, "Did this version work?"),
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
    const again = () => renderHistory(params);
    const events = [
      ...data.versions.map((v) => ({ at: v.first_seen, node: el("li", { class: "version" },
        el("div", { class: "meta" }, when(v.first_seen), outcomeBadge(v.outcome)),
        el("p", {}, v.current ? "Current version" : "Version", el("code", {}, " " + v.text_sha256.slice(0, 8)),
          v.available ? null : el("span", { class: "muted" }, " (text not stored)")),
        quickOutcome({ file: f.id, version: v.text_sha256 }, again, "Did this version work?")) })),
      ...data.moves.map((m) => ({ at: m.at, node: el("li", { class: "move" },
        el("div", { class: "meta" }, when(m.at)), el("p", {}, "Moved from " + m.from)) })),
      ...data.entries.map((e) => ({ at: e.created_at, node: el("li", {}, entryCard(e, f.id, again)) })),
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

function microsoftPanel() {
  const box = el("section", { class: "card microsoft", "aria-labelledby": "ms-h", "aria-live": "polite" });
  let timer = null;
  const start = async (deviceCode) => {
    try { await api("/microsoft/sign-in", { body: { device_code: deviceCode } }); }
    catch (error) { showError(box, error); return; }
    poll();
  };
  const idle = () => box.replaceChildren(
    el("h2", { id: "ms-h" }, "Microsoft OneDrive and SharePoint"),
    el("div", { class: "actions" },
      el("button", { type: "button", onclick: () => start(false) }, "Add Microsoft storage"),
      el("button", { type: "button", class: "secondary", onclick: () => start(true) }, "Sign in with a code")));
  const poll = async () => {
    clearTimeout(timer);
    let s;
    try { s = await api("/microsoft/sign-in"); } catch (error) { showError(box, error); return; }
    if (s.state === "waiting") {
      box.replaceChildren(el("h2", { id: "ms-h" }, "Signing in to Microsoft…"),
        el("p", {}, s.message || "Finish signing in in the browser window that opened."));
      timer = setTimeout(poll, 1000);
    } else if (s.state === "done") {
      box.replaceChildren(el("h2", { id: "ms-h" }, "Signed in as " + s.username),
        el("ul", { class: "folders" }, s.drives.map((d) => {
          const add = el("button", { type: "button", disabled: d.connected, onclick: async () => {
            add.disabled = true;
            try { await api("/microsoft/storage", { body: { drive_id: d.id } }); renderStorage(); }
            catch (error) { add.disabled = false; alert(error.message); }
          } }, d.connected ? "Connected" : "Add");
          return el("li", {}, el("span", {}, d.label), add);
        })),
        el("p", { class: "muted" }, "Follow a SharePoint site to see its libraries here."),
        el("button", { type: "button", class: "link", onclick: idle }, "Use another account"));
    } else if (s.state === "approval") {
      const link = el("input", { value: s.consent_url, readonly: true, "aria-label": "Approval link for IT" });
      box.replaceChildren(el("h2", { id: "ms-h" }, "Your university needs to approve this app"),
        el("p", {}, "Send IT this link, with docs/IT_APPROVAL.md from Eridi:"),
        el("div", { class: "inline-form" }, link,
          el("button", { type: "button", class: "secondary", onclick: async () => {
            link.select();
            try { await navigator.clipboard.writeText(s.consent_url); } catch (error) { /* selected for copying */ }
          } }, "Copy link")),
        el("button", { type: "button", class: "link", onclick: idle }, "Back"));
    } else if (s.state === "error") {
      box.replaceChildren(el("h2", { id: "ms-h" }, "Microsoft OneDrive and SharePoint"),
        el("p", { class: "error", role: "alert" }, s.message),
        el("button", { type: "button", class: "link", onclick: idle }, "Try again"));
    } else idle();
  };
  idle();
  poll();
  return box;
}

function googlePanel() {
  const box = el("section", { class: "card google", "aria-labelledby": "gd-h", "aria-live": "polite" });
  let timer = null;
  const idle = () => box.replaceChildren(
    el("h2", { id: "gd-h" }, "Google Drive"),
    el("div", { class: "actions" }, el("button", { type: "button", onclick: start }, "Add Google Drive")));
  async function start() {
    try { await api("/google/sign-in", { method: "POST" }); } catch (error) { showError(box, error); return; }
    poll();
  }
  const poll = async () => {
    clearTimeout(timer);
    let s;
    try { s = await api("/google/sign-in"); } catch (error) { showError(box, error); return; }
    if (s.state === "waiting") {
      box.replaceChildren(el("h2", { id: "gd-h" }, "Signing in to Google…"),
        el("p", {}, "Finish signing in in the browser window that opened."));
      timer = setTimeout(poll, 1000);
    } else if (s.state === "done") {
      box.replaceChildren(el("h2", { id: "gd-h" }, "Signed in as " + s.email),
        el("ul", { class: "folders" }, s.drives.map((d) => {
          const add = el("button", { type: "button", disabled: d.connected, onclick: async () => {
            add.disabled = true;
            try { await api("/google/storage", { body: { drive_id: d.id } }); renderStorage(); }
            catch (error) { add.disabled = false; alert(error.message); }
          } }, d.connected ? "Connected" : "Add");
          return el("li", {}, el("span", {}, d.label), add);
        })),
        el("button", { type: "button", class: "link", onclick: idle }, "Use another account"));
    } else if (s.state === "error") {
      box.replaceChildren(el("h2", { id: "gd-h" }, "Google Drive"),
        el("p", { class: "error", role: "alert" }, s.message),
        el("button", { type: "button", class: "link", onclick: idle }, "Try again"));
    } else idle();
  };
  idle();
  poll();
  return box;
}

async function allowSave(path, box) {
  try { await api(path + "/allow-save", { body: { device_code: false } }); }
  catch (error) { alert(error.message); return; }
  box.disabled = true;
  const timer = setInterval(async () => {
    const s = await api("/microsoft/sign-in").catch(() => null);
    if (!s || s.state === "waiting") return;
    clearInterval(timer);
    if (s.state !== "done") alert(s.message || "Sign-in didn't finish.");
    renderStorage();
  }, 1500);
}

async function renderStorage() {
  main.replaceChildren(el("h1", {}, "Storage"));
  const list = el("div", {});
  main.append(list, microsoftPanel(), googlePanel());
  try {
    const data = await api("/storage");
    if (!data.storage.length) {
      list.append(el("p", { class: "muted" }, "No storage yet. Add Microsoft storage below, or a folder in a terminal: eridi storage add <folder>"));
      return;
    }
    list.append(el("ul", { class: "storage" }, data.storage.map((root, i) => {
      const toggle = (field, label) => {
        const id = `${field}-${i}`;
        const box = el("input", { type: "checkbox", id, checked: root[field], onchange: async () => {
          const path = "/storage/" + encodeURIComponent(root.id);
          try { Object.assign(root, await api(path, { method: "PATCH", body: { [field]: box.checked } })); }
          catch (error) {
            box.checked = !box.checked;
            if (field === "save" && root.kind === "msgraph" && confirm("Saving to Microsoft needs your permission for Eridi to write there. Open the Microsoft sign-in?")) {
              allowSave(path, box);
            } else alert(error.message);
          }
        } });
        return el("label", { for: id, class: "switch" }, box, " " + label);
      };
      const mark = root.kind === "local" && !root.marked ? el("button", { type: "button", class: "secondary",
        title: "Writes .eridi-root.json into the folder, so your other computers and labmates recognize it",
        onclick: async () => {
          try { await api("/storage/" + encodeURIComponent(root.id) + "/mark", { method: "POST" }); renderStorage(); }
          catch (error) { alert(error.message); }
        } }, "Mark for other computers") : null;
      const approvedId = `approved-${i}`;
      const approved = el("input", { type: "checkbox", id: approvedId,
        checked: root.approved_labels.includes(PATIENT_DATA), onchange: async () => {
          const labels = root.approved_labels.filter((x) => x !== PATIENT_DATA);
          if (approved.checked) labels.push(PATIENT_DATA);
          try { Object.assign(root, await api("/storage/" + encodeURIComponent(root.id), { method: "PATCH", body: { approved_labels: labels } })); }
          catch (error) { approved.checked = !approved.checked; alert(error.message); }
        } });
      return el("li", { class: "card" },
        el("h2", {}, root.label),
        el("div", { class: "meta" }, { msgraph: "Microsoft", gdrive: "Google Drive" }[root.kind] || root.location),
        el("div", { class: "actions" }, toggle("search", "Search"), toggle("save", "Save"),
          el("label", { for: approvedId, class: "switch" }, approved, " Approved for patient data"), mark));
    })));
  } catch (error) { showError(list, error); }
}

// --- projects ---

const projectList = document.getElementById("project-list");
let currentProject = null;

async function refreshProjects() {
  try {
    const data = await api("/projects");
    projectList.replaceChildren(...data.projects.map((p) =>
      el("li", {}, el("a", { href: "#/project?id=" + encodeURIComponent(p.id),
        "aria-current": p.id === currentProject ? "page" : null }, p.name))));
  } catch (error) { projectList.replaceChildren(); }
}

document.getElementById("new-project").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("new-project-name");
  const message = document.getElementById("new-project-error");
  try {
    const p = await api("/projects", { body: { name: input.value } });
    input.value = "";
    message.hidden = true;
    location.hash = "#/project?id=" + encodeURIComponent(p.id);
    refreshProjects();
  } catch (error) { message.hidden = false; message.textContent = error.message; }
});

async function renderProject(params) {
  const id = params.get("id");
  main.replaceChildren();
  if (!id) { main.append(el("p", { class: "muted" }, "Choose a project.")); return; }
  const base = "/projects/" + encodeURIComponent(id);
  let p;
  try { p = await api(base); } catch (error) { showError(main, error); return; }

  const message = el("p", { class: "error", role: "alert", hidden: true });
  const fail = (error) => { message.hidden = false; message.textContent = error.message; };
  const again = () => { renderProject(params); refreshProjects(); };

  const name = el("input", { id: "project-name", value: p.name, required: true, "aria-label": "Project name" });
  const renameForm = el("form", { class: "inline-form", hidden: true, onsubmit: async (e) => {
    e.preventDefault();
    try { await api(base, { method: "PATCH", body: { name: name.value } }); again(); } catch (error) { fail(error); }
  } }, name, el("button", { type: "submit" }, "Rename"));
  const sensitive = el("input", { type: "checkbox", id: "project-patient-data",
    checked: p.labels.includes(PATIENT_DATA), onchange: async () => {
      const labels = p.labels.filter((x) => x !== PATIENT_DATA);
      if (sensitive.checked) labels.push(PATIENT_DATA);
      try { await api(base, { method: "PATCH", body: { labels } }); again(); }
      catch (error) { sensitive.checked = !sensitive.checked; fail(error); }
    } });
  const heading = el("div", { class: "heading" }, el("h1", {}, p.name),
    el("label", { for: "project-patient-data", class: "switch" + (sensitive.checked ? " flag" : "") },
      sensitive, " Contains patient data"),
    el("button", { type: "button", class: "link", onclick: () => {
      heading.hidden = true; renameForm.hidden = false; name.focus(); name.select();
    } }, "Rename"));

  const folder = el("input", { id: "add-folder", required: true, "aria-label": "Folder to add",
    placeholder: "Folder path, or storage name" });
  const folders = el("section", { "aria-labelledby": "folders-h" },
    el("h2", { id: "folders-h" }, "Folders"),
    el("ul", { class: "folders" }, p.folders.map((f) => el("li", {},
      el("span", {}, (f.storage || f.storage_id) + (f.path ? " / " + f.path : "")),
      el("button", { type: "button", class: "link", onclick: async () => {
        try { await api(base + "/folders/remove", { body: { folder: f.ref } }); again(); } catch (error) { fail(error); }
      } }, "Remove")))),
    el("form", { class: "inline-form", onsubmit: async (e) => {
      e.preventDefault();
      try { await api(base + "/folders", { body: { folder: folder.value } }); again(); } catch (error) { fail(error); }
    } }, folder, el("button", { type: "submit" }, "Add folder")));

  const query = el("input", { type: "search", id: "project-q", "aria-label": "Search this project",
    placeholder: "Search " + p.name });
  const results = el("div", { "aria-live": "polite" });
  const search = el("form", { class: "searchbox", role: "search", onsubmit: async (e) => {
    e.preventDefault();
    const q = query.value.trim();
    if (!q) { results.replaceChildren(); return; }
    try {
      const data = await api("/search?q=" + encodeURIComponent(q) + "&project=" + encodeURIComponent(p.id));
      results.replaceChildren(data.results.length ? el("ul", { class: "results" }, data.results.map((r) => {
        const isFile = r.type === "file";
        return el("li", { class: "result " + r.type },
          el("div", { class: "title" }, el("span", { class: "tag" }, isFile ? "File" : "Note"),
            r.file_id ? historyLink(r.file_id, r.title) : el("span", {}, r.title)),
          el("div", { class: "meta" }, [r.storage, r.path].filter(Boolean).join(" · ")),
          snippet(r.snippet));
      })) : el("p", { class: "muted" }, "Nothing found."));
    } catch (error) { showError(results, error); }
  } }, query, el("button", { type: "submit" }, "Search"));

  const note = el("textarea", { id: "project-note", rows: 3, required: true });
  const outcome = outcomeSelect("project-note-outcome");
  const noteForm = el("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    if (!note.value.trim()) { note.focus(); return; }
    try {
      await api("/notes", { body: { text: note.value, project: p.id, outcome: outcome.value || null } });
      again();
    } catch (error) { fail(error); }
  } },
    el("label", { for: "project-note" }, "Add a note"), note,
    el("div", { class: "actions" },
      el("label", { for: "project-note-outcome", class: "visually-hidden" }, "Outcome"), outcome,
      el("button", { type: "submit" }, "Save")));

  const saving = savePanel(p, fail);
  const views = viewsPanel(p);

  const changes = p.changes.length ? el("section", { "aria-labelledby": "changes-h" },
    el("h2", { id: "changes-h" }, "Needs a why"),
    p.changes.map((c, i) => changeCard(c, "p" + i))) : null;

  const files = el("section", { "aria-labelledby": "files-h" },
    el("h2", { id: "files-h" }, `Files (${p.file_count})`),
    p.file_count ? el("ul", { class: "filelist" }, p.files.map((f) => el("li", {},
      historyLink(f.id, f.path), el("span", { class: "muted" }, f.storage ? " · " + f.storage : ""))))
      : el("p", { class: "muted" }, p.folders.length ? "No files found yet. Try Scan now." : "Add a folder to see its files."));

  const timeline = el("section", { "aria-labelledby": "record-h" },
    el("h2", { id: "record-h" }, "Record"),
    el("ol", { class: "timeline" }, p.entries.map((e) => el("li", {},
      e.kind === "project" ? el("p", { class: "muted" }, `Project "${e.project_name}" · ${e.author} · ${when(e.created_at)}`)
        : entryCard(e, null, again)))));

  main.append(...[heading, renameForm, message, search, results, views, changes, saving, folders, files, noteForm, timeline]
    .filter(Boolean));
}

// --- the "did it work?" views ---

function viewsPanel(p) {
  const panel = el("div", { class: "view-panel", "aria-live": "polite" });
  const q = "project=" + encodeURIComponent(p.id);
  const tabs = el("div", { class: "tabs", role: "group", "aria-label": "Views" });
  const show = async (button, load) => {
    tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    try { panel.replaceChildren(await load()); } catch (error) { showError(panel, error); }
  };
  const tab = (label, load) => {
    const b = el("button", { type: "button", class: "secondary", "aria-pressed": "false", onclick: () => show(b, load) }, label);
    tabs.append(b);
  };
  const empty = (text) => el("p", { class: "muted" }, text);
  tab("This month", async () => {
    const d = await api("/views/month?" + q);
    const c = d.counts;
    return el("div", {},
      el("p", { class: "meta" }, `${d.period.label} · ${c.edited} edited · ${c.added} added · ${c.needs_why} need a why · ${c.worked} worked · ${c.partial} partly · ${c.failed} failed`),
      d.changes.length || d.entries.length ? el("ul", { class: "results" },
        d.changes.map((a) => el("li", { class: "result" },
          el("div", { class: "title" }, el("span", { class: "tag" }, a.kind === "edited" ? "Edited" : "Added"), historyLink(a.file_id, a.path),
            a.needs_why ? el("span", { class: "outcome" }, "needs a why") : null),
          a.summary ? el("div", { class: "meta" }, a.summary) : null,
          a.explanations.map((e) => el("p", {}, e.text, " ", outcomeBadge(e.result))))),
        d.entries.filter((e) => e.kind !== "project").map((e) => el("li", {}, entryCard(e, null))))
        : empty("Nothing changed this month."));
  });
  tab("Failed", async () => {
    const d = await api("/views/failed?" + q);
    return d.failures.length ? el("ul", { class: "results" }, d.failures.map((f) => el("li", { class: "result" },
      el("div", { class: "title" }, f.file ? historyLink(f.file, f.path) : el("span", {}, f.entry.kind)),
      entryCard(f.entry, f.file),
      f.judged_by.id !== f.entry.id && f.judged_by.text ? el("p", { class: "meta" }, "Failed: " + f.judged_by.text + " — " + f.judged_by.author) : null)))
      : empty("Nothing recorded as failed.");
  });
  tab("Open questions", async () => {
    const d = await api("/views/questions?" + q);
    const again = () => tabs.querySelectorAll("button")[2].click();
    return d.questions.length ? el("ul", { class: "results" }, d.questions.map((item) => el("li", { class: "result" },
      item.kind === "no_why"
        ? el("div", {}, el("div", { class: "title" }, historyLink(item.change.file_id, item.change.path), el("span", { class: "outcome" }, "needs a why")),
            el("div", { class: "meta" }, (item.change.summary || "Text changed") + " · " + ago(item.since)))
        : el("div", {}, el("div", { class: "meta" }, ago(item.since)), entryCard(item.entry, null, again)))))
      : empty(`No open questions older than ${d.days} days.`);
  });
  return el("section", { "aria-labelledby": "views-h" }, el("h2", { id: "views-h", class: "visually-hidden" }, "Views"), tabs, panel);
}

// --- saving files into a project (outbox) ---

const SAVE_STATES = {
  saved: "Saved on this computer", queued: "Waiting to upload", uploading: "Uploading",
  uploaded: "Uploaded", needs_confirmation: "Name taken", blocked: "Not allowed",
};

function saveItem(job, refresh) {
  const act = (label, path, body) => el("button", { type: "button", class: "secondary", onclick: async () => {
    try { await api("/outbox/" + encodeURIComponent(job.id) + path, { method: "POST", body }); refresh(); }
    catch (error) { alert(error.message); }
  } }, label);
  const actions = [];
  if (job.state === "needs_confirmation") {
    actions.push(act("Replace", "/retry", { on_exists: "replace" }), act("Save as new name", "/retry", { on_exists: "rename" }));
  } else if (job.state === "queued" || job.state === "blocked") {
    actions.push(act("Try again", "/retry", {}));
  }
  if (job.state !== "uploaded") actions.push(act("Cancel", "/cancel"));
  return el("li", { class: "save " + job.state },
    el("span", { class: "tag state" }, SAVE_STATES[job.state] || job.state),
    job.file ? historyLink(job.file, job.path) : el("span", {}, job.path),
    job.error && job.state !== "uploaded" ? el("div", { class: "meta" }, job.error) : null,
    actions.length ? el("div", { class: "actions" }, actions) : null);
}

function savePanel(p, fail) {
  const list = el("ul", { class: "saves", "aria-live": "polite" });
  let timer = null;
  async function refresh() {
    clearTimeout(timer);
    if (!list.isConnected && timer !== null) return;
    try {
      const data = await api("/outbox");
      const mine = data.saves.filter((j) => j.project_id === p.id).slice(0, 20);
      list.replaceChildren(...mine.map((j) => saveItem(j, refresh)));
      if (mine.some((j) => ["saved", "queued", "uploading"].includes(j.state))) timer = setTimeout(refresh, 3000);
      else timer = 0;
    } catch (error) { showError(list, error); }
  }
  const input = el("input", { type: "file", id: "save-file", multiple: true, "aria-label": "Files to save" });
  const form = el("form", { class: "inline-form", onsubmit: async (e) => {
    e.preventDefault();
    for (const file of input.files) {
      const query = "?name=" + encodeURIComponent(file.name) + "&project=" + encodeURIComponent(p.id);
      const response = await fetch("/api/save" + query, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/octet-stream" }, body: file });
      if (!response.ok) { const data = await response.json().catch(() => ({})); fail(new Error(data.error || response.statusText)); }
    }
    input.value = "";
    refresh();
  } }, input, el("button", { type: "submit" }, "Save to " + (p.home_storage || "storage")));
  refresh();
  return el("section", { "aria-labelledby": "saves-h" }, el("h2", { id: "saves-h" }, "Save a file"),
    p.home_storage_id ? form : homePicker(p, fail), list);
}

function homePicker(p, fail) {
  const select = el("select", { id: "home-storage", "aria-label": "Home storage" },
    el("option", { value: "" }, "Choose where this project saves…"));
  api("/storage").then((data) => select.append(...data.storage.map((r) =>
    el("option", { value: r.id }, r.label + (r.save ? "" : " (Save off)"))))).catch(fail);
  select.addEventListener("change", async () => {
    if (!select.value) return;
    try { await api("/projects/" + encodeURIComponent(p.id), { method: "PATCH", body: { home_storage_id: select.value } }); route(); }
    catch (error) { fail(error); }
  });
  return select;
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
  currentProject = name === "project" ? params.get("id") : null;
  refreshProjects();
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (name === "changes") renderChanges();
  else if (name === "activity") renderActivity();
  else if (name === "history") renderHistory(params);
  else if (name === "storage") renderStorage();
  else if (name === "project") renderProject(params);
  else renderSearch(params);
}

window.addEventListener("hashchange", route);
route();
refreshStatus();
setInterval(refreshStatus, 5000);
