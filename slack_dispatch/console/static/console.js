"use strict";

// Every user-visible string of the console lives in this one object.
const UI = Object.freeze({
  title: "Dispatch console",
  navLabel: "Views",
  tabQueue: "Triage",
  tabLanes: "Lanes",
  tabService: "Service",
  tabProposals: "Scope",
  proposalsTitle: (count, min) => count + " scope proposal(s); suggested after " + min + " consistent event(s)",
  proposalsEmpty: "No open or suggested scope proposals.",
  confirm: "Confirm",
  reject: "Reject",
  defer: "Defer",
  conflict: "conflict",
  effective: "in effect",
  imported: "imported",
  current: (lane, source) => lane + " (" + source + ")",
  proposalDone: (status) => "Proposal " + status,
  learnedTitle: "Learned scope",
  learnedEmpty: "No confirmed scope overrides in the ledger.",
  exportButton: "Export YAML",
  exportHint: "Merge into the instance config; the ledger override is already in effect.",
  searchPlaceholder: "Search id, channel, author or text",
  searchButton: "Search",
  loading: "Loading...",
  loadFailed: "Could not load data from the console server.",
  refreshed: (when) => "Updated " + when,
  refresh: "Refresh",
  queueTitle: (total) => total + " item(s) waiting for a lane",
  queueEmpty: "The human queue is empty.",
  showing: (first, last, total) => "Showing " + first + "-" + last + " of " + total,
  previous: "Previous",
  next: "Next",
  selectAll: "Select all on this page",
  selected: (count) => count + " selected",
  assignSelected: "Assign selected to",
  assign: "Assign",
  direct: "Direct",
  directTitle: (lane) => "Assign to " + lane,
  exclude: "Exclude",
  done: "Done",
  notePlaceholder: "Optional note",
  noBody: "No body stored",
  rootLabel: "Thread root",
  rootMissing: "Root not in ledger",
  truncated: "(truncated)",
  context: (shown, total) => "Thread context: last " + shown + " of " + total + " message(s)",
  later: "later",
  suggested: (lane, reason) => "Suggested: " + lane + (reason ? " (" + reason + ")" : ""),
  noSuggestion: "No classifier suggestion",
  pendingReason: (reason) => "Queued because: " + reason,
  hints: "Hints:",
  hintRule: (channel) => "rule for " + channel,
  hintHistory: (count) => count + " recent",
  attention: (flag) => "attention: " + flag,
  origin: (value) => "origin: " + value,
  edited: "edited",
  files: "files",
  bot: "bot",
  reply: "reply",
  age: (text) => text + " ago",
  actionApplied: (count) => count + " item(s) updated",
  actionFailed: (count, detail) => count + " item(s) not updated: " + detail,
  requestFailed: (detail) => "Request failed: " + detail,
  lanesTitle: "Lanes",
  openInbox: "Open inbox",
  session: "Session",
  noSession: "no session",
  addressedBy: (kind) => "(" + kind + ")",
  live: "live",
  notLive: "not live",
  paused: "paused",
  unregistered: "not in ledger",
  today: "Today",
  todayCounts: (c) => "new " + c.new + ", assigned " + c.assigned + ", acked " + c.acked + ", done " + c.done,
  open: "Open items",
  oldestUnacked: "Oldest unacknowledged",
  lastDelivery: "Last delivery",
  lastReminder: "Last reminder wake",
  inboxFile: "Inbox file",
  none: "-",
  back: "Back to lanes",
  laneInbox: (lane) => "Lane " + lane + " inbox",
  fullRoots: "Show full root bodies",
  laneEmpty: "No pending items in this lane.",
  ownerReplied: (when) => "Owner replied at " + when,
  ack: "Ack",
  reroute: "Reroute",
  rerouteTo: "Reroute to",
  state: (value) => "state: " + value,
  kind: (value) => "kind: " + value,
  serviceTitle: "Service",
  heartbeat: "Heartbeat",
  noHeartbeat: "No heartbeat file found.",
  stale: "stale",
  fresh: "fresh",
  counters: "Counters",
  wake: "Wake switch",
  wakeOff: "wake.off present: wakes are blocked",
  wakeOn: "wake.off absent",
  wakeConfig: (enabled) => "wake_policy.enabled: " + enabled,
  humanQueue: "Human queue",
  adapters: "Notification adapters",
  reconcile: "Recent reconcile passes",
  noReconcile: "No reconcile pass in the current log.",
  warnings: "Recent warnings and errors",
  noWarnings: "No warnings or errors in the current log.",
  gaps: "Ingest gaps",
  noGaps: "No ingest gaps recorded.",
  searchTitle: (query) => "Search: " + query,
  searchEmpty: "No matching messages.",
  searchTruncated: "Showing the first 200 matches.",
  close: "Close",
  columns: {
    id: "Id", when: "When", channel: "Channel", author: "Author", state: "State", lane: "Lane",
    reason: "Route reason", note: "Note", text: "Text", index: "#", kind: "Kind",
    purpose: "Purpose", dryRun: "Dry run", at: "At", counts: "Counts", level: "Level",
    logger: "Logger", message: "Message", source: "Source", detected: "Detected",
    lastEvent: "Last event", resumed: "Resumed", catchup: "Caught up", key: "Field", value: "Value",
    scopeKey: "Key", name: "Name", evidence: "Evidence", firstSeen: "First seen", lastSeen: "Last seen",
    status: "Status", lanes: "Lanes", current: "Current default", actions: "Actions", by: "By",
    inConfig: "In config",
  },
  yes: "yes",
  no: "no",
  units: { s: "s", m: "m", h: "h", d: "d", bytes: "B", kib: "KiB", mib: "MiB" },
});

const VIEWS = { "/": "queue", "/lanes": "lanes", "/service": "service", "/proposals": "proposals" };
const state = {
  config: null,
  view: "queue",
  queueOffset: 0,
  queueCards: new Map(),
  selected: new Set(),
  pendingMsg: null,
  lane: null,
  laneFull: false,
  previousView: "queue",
};

// ---------- helpers ----------

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "className") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function age(seconds) {
  if (seconds === null || seconds === undefined) return UI.none;
  const u = UI.units;
  if (seconds < 60) return seconds + u.s;
  if (seconds < 3600) return Math.floor(seconds / 60) + u.m;
  if (seconds < 86400) return Math.floor(seconds / 3600) + u.h + " " + Math.floor((seconds % 3600) / 60) + u.m;
  return Math.floor(seconds / 86400) + u.d + " " + Math.floor((seconds % 86400) / 3600) + u.h;
}

function bytes(value) {
  if (value === null || value === undefined) return UI.none;
  const u = UI.units;
  if (value < 1024) return value + " " + u.bytes;
  if (value < 1048576) return (value / 1024).toFixed(1) + " " + u.kib;
  return (value / 1048576).toFixed(1) + " " + u.mib;
}

function named(ref) {
  if (!ref || !ref.id) return UI.none;
  return ref.name ? ref.name : ref.id;
}

function bodyBlock(text, truncated) {
  if (text === null || text === undefined) return el("div", { className: "body empty", text: UI.noBody });
  return el("div", { className: "body" }, text, truncated ? " " + UI.truncated : null);
}

let statusTimer = null;
function status(message, isError) {
  const node = document.getElementById("status");
  node.textContent = message || "";
  node.className = "status" + (message ? " show" : "") + (isError ? " error" : "");
  clearTimeout(statusTimer);
  if (message) statusTimer = setTimeout(() => status(""), isError ? 12000 : 5000);
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error(UI.loadFailed);
  }
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : String(response.status);
    throw new Error(UI.requestFailed(detail));
  }
  return data;
}

function reportResults(data) {
  const failed = data.results.filter((row) => !row.ok);
  if (failed.length) {
    const detail = failed.map((row) => row.id + " " + row.error + (row.detail ? " (" + row.detail + ")" : "")).join("; ");
    status(UI.actionFailed(failed.length, detail), true);
  } else {
    status(UI.actionApplied(data.applied));
  }
  return new Set(data.results.filter((row) => row.ok).map((row) => row.id));
}

function nowText() {
  const now = new Date();
  return [now.getHours(), now.getMinutes(), now.getSeconds()].map((part) => String(part).padStart(2, "0")).join(":");
}

// ---------- views and routing ----------

function showView(name, push) {
  state.view = name;
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== "view-" + name;
  for (const link of document.querySelectorAll(".tabs a")) link.classList.toggle("active", link.dataset.view === name);
  if (push) {
    const path = Object.keys(VIEWS).find((key) => VIEWS[key] === name);
    if (path && location.pathname !== path) history.pushState({}, "", path);
  }
  refresh();
}

function refresh() {
  if (state.view === "queue") return loadQueue();
  if (state.view === "lanes") return state.lane ? loadLaneInbox() : loadLanes();
  if (state.view === "service") return loadService();
  if (state.view === "proposals") return loadProposals();
  return null;
}

// ---------- triage queue ----------

function queueRoot() {
  const section = document.getElementById("view-queue");
  if (!section.querySelector(".queue-list")) {
    const header = el("div", { className: "toolbar" },
      el("strong", { className: "queue-total", text: UI.loading }),
      el("span", { className: "muted small queue-updated" }),
      el("span", { className: "spacer" }),
      el("span", { className: "muted small queue-range" }),
      el("button", { className: "queue-prev", text: UI.previous, onclick: () => pageQueue(-1) }),
      el("button", { className: "queue-next", text: UI.next, onclick: () => pageQueue(1) }),
      el("button", { text: UI.refresh, onclick: () => loadQueue() }));
    section.append(header, bulkBar(), el("div", { className: "queue-list" }));
  }
  return section;
}

function bulkBar() {
  const laneSelect = el("select", { className: "bulk-lane" });
  const note = el("input", { type: "text", className: "bulk-note", placeholder: UI.notePlaceholder, maxlength: "2000" });
  const selectAll = el("input", { type: "checkbox", className: "bulk-all", title: UI.selectAll, "aria-label": UI.selectAll,
    onchange: (event) => {
      for (const [id, card] of state.queueCards) {
        card.check.checked = event.target.checked;
        if (event.target.checked) state.selected.add(id); else state.selected.delete(id);
      }
      updateBulk();
    } });
  const ids = () => Array.from(state.selected);
  return el("div", { className: "bulk" },
    selectAll,
    el("span", { className: "bulk-count", text: UI.selected(0) }),
    el("span", { text: UI.assignSelected }), laneSelect,
    el("button", { className: "bulk-action", text: UI.assign, onclick: () => queueAction("assign", ids(), laneSelect.value) }),
    el("button", { className: "bulk-action bulk-direct", text: UI.direct, onclick: () => queueAction("direct", ids()) }),
    el("button", { className: "bulk-action danger", text: UI.exclude, onclick: () => queueAction("exclude", ids()) }),
    note,
    el("button", { className: "bulk-action", text: UI.done, onclick: () => queueAction("done", ids(), null, note.value) }));
}

function updateBulk() {
  const section = queueRoot();
  section.querySelector(".bulk-count").textContent = UI.selected(state.selected.size);
  for (const button of section.querySelectorAll(".bulk-action")) button.disabled = state.selected.size === 0;
  const direct = section.querySelector(".bulk-direct");
  direct.hidden = !state.config.direct_lane;
  if (state.config.direct_lane) direct.title = UI.directTitle(state.config.direct_lane);
  const select = section.querySelector(".bulk-lane");
  if (!select.options.length) for (const lane of state.config.lanes) select.append(el("option", { value: lane, text: lane }));
  section.querySelector(".bulk-all").checked = state.queueCards.size > 0 && state.selected.size === state.queueCards.size;
}

function pageQueue(step) {
  const size = state.config.page_size;
  state.queueOffset = Math.max(0, state.queueOffset + step * size);
  loadQueue();
}

async function loadQueue() {
  const section = queueRoot();
  let data;
  try {
    data = await api("/api/queue?offset=" + state.queueOffset + "&limit=" + state.config.page_size);
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (data.total && data.offset >= data.total) {
    state.queueOffset = Math.max(0, data.total - (data.total % data.limit || data.limit));
    return loadQueue();
  }
  section.querySelector(".queue-total").textContent = data.total ? UI.queueTitle(data.total) : UI.queueEmpty;
  section.querySelector(".queue-updated").textContent = UI.refreshed(nowText());
  const first = data.items.length ? data.offset + 1 : 0;
  section.querySelector(".queue-range").textContent = data.total ? UI.showing(first, data.offset + data.items.length, data.total) : "";
  section.querySelector(".queue-prev").disabled = data.offset === 0;
  section.querySelector(".queue-next").disabled = data.offset + data.items.length >= data.total;
  renderQueue(section.querySelector(".queue-list"), data.items);
  updateBulk();
  focusPendingMessage();
}

function renderQueue(list, items) {
  const wanted = new Set(items.map((item) => item.id));
  for (const [id, card] of state.queueCards) {
    if (!wanted.has(id)) {
      card.node.remove();
      state.queueCards.delete(id);
      state.selected.delete(id);
    }
  }
  let ref = list.firstChild;
  for (const item of items) {
    let card = state.queueCards.get(item.id);
    if (card) {
      card.age.textContent = UI.age(age(item.age_seconds));
    } else {
      card = queueCard(item);
      state.queueCards.set(item.id, card);
    }
    if (card.node === ref) ref = ref.nextSibling;
    else list.insertBefore(card.node, ref);
  }
}

function laneOrder(item) {
  const lanes = state.config.lanes.slice();
  const suggested = item.suggestion && lanes.includes(item.suggestion.lane) ? item.suggestion.lane : null;
  return suggested ? [suggested].concat(lanes.filter((lane) => lane !== suggested)) : lanes;
}

function queueCard(item) {
  const check = el("input", { type: "checkbox", "aria-label": item.id,
    onchange: (event) => {
      if (event.target.checked) state.selected.add(item.id); else state.selected.delete(item.id);
      updateBulk();
    } });
  const ageNode = el("span", { text: UI.age(age(item.age_seconds)) });
  const meta = el("div", { className: "meta" },
    check,
    el("strong", { text: item.when || item.ts }),
    el("span", { title: item.channel.id, text: "#" + named(item.channel) }),
    el("span", { title: item.author.id || "", text: named(item.author) }),
    item.attention ? el("span", { className: "badge attention", text: UI.attention(item.attention) }) : null,
    item.is_reply ? el("span", { className: "badge", text: UI.reply }) : null,
    item.origin ? el("span", { className: "badge", text: UI.origin(item.origin) }) : null,
    item.edited ? el("span", { className: "badge warn", text: UI.edited }) : null,
    item.has_files ? el("span", { className: "badge", text: UI.files }) : null,
    item.is_bot ? el("span", { className: "badge", text: UI.bot }) : null,
    ageNode,
    el("span", { className: "mono small", text: item.id }));
  const parts = [meta];
  if (item.root) {
    parts.push(el("div", { className: "root" },
      el("div", { className: "meta small" }, el("strong", { text: UI.rootLabel }),
        item.root.missing ? el("span", { text: UI.rootMissing }) : [
          el("span", { title: item.root.author.id || "", text: named(item.root.author) }),
          el("span", { text: item.root.when || "" })]),
      item.root.missing ? null : bodyBlock(item.root.body, item.root.truncated)));
  }
  parts.push(bodyBlock(item.body, false));
  if (item.context.length) {
    const total = Math.max(0, item.thread_size - 1 - (item.root && !item.root.missing ? 1 : 0));
    parts.push(el("details", { className: "context" },
      el("summary", { text: UI.context(item.context.length, total) }),
      item.context.map((row) => el("div", { className: "context-item" + (row.after ? " after" : "") },
        el("div", { className: "meta small" },
          el("span", { title: row.author.id || "", text: named(row.author) }),
          el("span", { text: row.when || "" }),
          row.after ? el("span", { className: "badge", text: UI.later }) : null,
          row.state ? el("span", { className: "badge", text: UI.state(row.state) }) : null),
        bodyBlock(row.body, row.truncated)))));
  }
  const hintLanes = new Map(item.hints.map((hint) => [hint.lane, hint]));
  const suggestText = item.suggestion ? UI.suggested(item.suggestion.lane, item.suggestion.reason) : UI.noSuggestion;
  parts.push(el("div", { className: "suggest small" },
    suggestText,
    item.pending_reason ? " / " + UI.pendingReason(item.pending_reason) : null,
    item.hints.length ? " / " + UI.hints + " " + item.hints.map((hint) => hint.lane + " (" +
      (hint.source === "rule" ? UI.hintRule(hint.channel) : UI.hintHistory(hint.count)) + ")").join(", ") : null));
  const note = el("input", { type: "text", placeholder: UI.notePlaceholder, maxlength: "2000" });
  const laneButtons = laneOrder(item).map((lane) => {
    const suggested = item.suggestion && item.suggestion.lane === lane;
    const hint = hintLanes.get(lane);
    return el("button", { className: suggested ? "suggested" : (hint ? "hinted" : ""), text: lane,
      onclick: () => queueAction("assign", [item.id], lane) });
  });
  parts.push(el("div", { className: "actions" },
    laneButtons,
    state.config.direct_lane ? el("button", { className: "primary", text: UI.direct, title: UI.directTitle(state.config.direct_lane),
      onclick: () => queueAction("direct", [item.id]) }) : null,
    el("button", { className: "danger", text: UI.exclude, onclick: () => queueAction("exclude", [item.id]) }),
    note,
    el("button", { text: UI.done, onclick: () => queueAction("done", [item.id], null, note.value) })));
  const node = el("article", { className: "card", "data-id": item.id }, parts);
  return { node, check, age: ageNode };
}

async function queueAction(action, ids, lane, note) {
  if (!ids.length) return;
  let data;
  try {
    data = await api("/api/queue/actions", { action, ids, lane: lane || null, note: note || null });
  } catch (error) {
    status(error.message, true);
    return;
  }
  const done = reportResults(data);
  for (const id of done) {
    const card = state.queueCards.get(id);
    if (card) card.node.remove();
    state.queueCards.delete(id);
    state.selected.delete(id);
  }
  updateBulk();
  loadQueue();
}

function focusPendingMessage() {
  if (!state.pendingMsg) return;
  const id = state.pendingMsg;
  state.pendingMsg = null;
  const card = state.queueCards.get(id);
  if (card) {
    card.node.scrollIntoView({ block: "center" });
    card.node.classList.add("flash");
    setTimeout(() => card.node.classList.remove("flash"), 4000);
  } else {
    runSearch(id);
  }
}

// ---------- lanes ----------

function laneSection() {
  return document.getElementById("view-lanes");
}

async function loadLanes() {
  let data;
  try {
    data = await api("/api/lanes");
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (state.lane) return;
  const section = clear(laneSection());
  section.append(el("div", { className: "toolbar" },
    el("strong", { text: UI.lanesTitle }),
    el("span", { className: "muted small", text: UI.refreshed(nowText()) }),
    el("span", { className: "spacer" }),
    el("button", { text: UI.refresh, onclick: () => loadLanes() })));
  section.append(el("div", { className: "grid" }, data.lanes.map(laneCard)));
}

function laneCard(card) {
  const session = card.session;
  const rows = [
    [UI.session, el("span", {},
      el("span", { className: "dot" + (session.live ? " live" : "") }),
      session.value ? session.value + " " + UI.addressedBy(session.addressed_by) + " " + (session.live ? UI.live : UI.notLive) : UI.noSession)],
    [UI.today, UI.todayCounts(card.today)],
    [UI.open, String(card.open)],
    [UI.oldestUnacked, age(card.oldest_unacked_seconds)],
    [UI.lastDelivery, card.last_delivery || UI.none],
    [UI.lastReminder, card.last_reminder || UI.none],
    [UI.inboxFile, bytes(card.inbox_bytes)],
  ];
  return el("article", { className: "card" },
    el("div", { className: "meta" },
      el("strong", { text: card.lane }),
      card.paused ? el("span", { className: "badge warn", text: UI.paused }) : null,
      card.registered ? null : el("span", { className: "badge bad", text: UI.unregistered })),
    el("dl", { className: "kv small" }, rows.map(([key, value]) => [el("dt", { text: key }), el("dd", {}, value)])),
    el("div", { className: "actions" }, el("button", { className: "primary", text: UI.openInbox, onclick: () => openLane(card.lane) })));
}

function openLane(lane) {
  state.lane = lane;
  loadLaneInbox();
}

function laneInputsBusy() {
  const active = document.activeElement;
  if (active && laneSection().contains(active) && (active.tagName === "INPUT" || active.tagName === "SELECT")) return true;
  return Array.from(laneSection().querySelectorAll("input[type=text]")).some((input) => input.value);
}

async function loadLaneInbox(force) {
  const lane = state.lane;
  if (!force && laneSection().querySelector(".lane-inbox") && laneInputsBusy()) return;
  let data;
  try {
    data = await api("/api/lanes/" + encodeURIComponent(lane) + "/inbox?full=" + (state.laneFull ? "true" : "false"));
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (state.lane !== lane) return;
  const section = clear(laneSection());
  const full = el("input", { type: "checkbox", onchange: (event) => { state.laneFull = event.target.checked; loadLaneInbox(true); } });
  full.checked = state.laneFull;
  section.append(el("div", { className: "toolbar lane-inbox" },
    el("button", { text: UI.back, onclick: () => { state.lane = null; loadLanes(); } }),
    el("strong", { text: UI.laneInbox(lane) }),
    el("span", { className: "muted small", text: UI.refreshed(nowText()) }),
    el("span", { className: "spacer" }),
    el("label", { className: "small" }, full, " " + UI.fullRoots),
    el("button", { text: UI.refresh, onclick: () => loadLaneInbox(true) })));
  if (!data.groups.length) {
    section.append(el("div", { className: "empty-state", text: UI.laneEmpty }));
    return;
  }
  for (const group of data.groups) section.append(laneGroup(lane, group));
}

function laneGroup(lane, group) {
  const root = group.root;
  const parts = [el("div", { className: "meta" },
    el("strong", { text: root.when_full || root.when || root.ts }),
    el("span", { title: root.channel, text: "#" + (root.channel_name || root.channel) }),
    el("span", { title: root.author_id || "", text: root.author }),
    el("span", { className: "badge", text: UI.state(root.state) }),
    root.missing ? el("span", { className: "badge warn", text: UI.rootMissing }) : null,
    root.pending && root.kind ? el("span", { className: "badge", text: UI.kind(root.kind) }) : null,
    root.pending && root.edited === "yes" ? el("span", { className: "badge warn", text: UI.edited }) : null,
    root.pending && root.attention && root.attention !== "-" ? el("span", { className: "badge attention", text: UI.attention(root.attention) }) : null,
    el("span", { className: "mono small", text: root.id }))];
  if ("body" in root) parts.push(el("div", { className: "body" }, root.body));
  if (root.pending) parts.push(laneActions(lane, root.id));
  for (const item of group.items) {
    parts.push(el("div", { className: "item" },
      el("div", { className: "meta small" },
        el("strong", { title: item.author_id || "", text: item.author }),
        el("span", { text: item.when_full || item.when }),
        el("span", { className: "badge", text: UI.state(item.state || UI.none) }),
        el("span", { className: "badge", text: UI.kind(item.kind) }),
        item.edited === "yes" ? el("span", { className: "badge warn", text: UI.edited }) : null,
        item.attention && item.attention !== "-" ? el("span", { className: "badge attention", text: UI.attention(item.attention) }) : null,
        el("span", { className: "mono", text: item.id })),
      "body" in item ? el("div", { className: "body" }, item.body) : null,
      laneActions(lane, item.id)));
  }
  if (group.owner_replied_at) {
    parts.push(el("div", { className: "suggest small" }, el("span", { className: "badge ok", text: UI.ownerReplied(group.owner_replied_when || group.owner_replied_at) })));
  }
  return el("article", { className: "card" }, parts);
}

function laneActions(lane, id) {
  const note = el("input", { type: "text", placeholder: UI.notePlaceholder, maxlength: "2000" });
  const target = el("select", { "aria-label": UI.rerouteTo },
    state.config.lanes.filter((other) => other !== lane).map((other) => el("option", { value: other, text: other })));
  return el("div", { className: "actions" },
    el("button", { text: UI.ack, onclick: () => laneAction(lane, "ack", id) }),
    note,
    el("button", { text: UI.done, onclick: () => laneAction(lane, "done", id, { note: note.value }) }),
    el("span", { className: "muted small", text: UI.rerouteTo }),
    target,
    el("button", { text: UI.reroute, onclick: () => laneAction(lane, "reroute", id, { lane: target.value }) }));
}

async function laneAction(lane, action, id, extra) {
  const body = Object.assign({ action, ids: [id], lane: null, note: null }, extra || {});
  if (body.note === "") body.note = null;
  let data;
  try {
    data = await api("/api/lanes/" + encodeURIComponent(lane) + "/actions", body);
  } catch (error) {
    status(error.message, true);
    return;
  }
  reportResults(data);
  loadLaneInbox(true);
}

// ---------- service ----------

function table(headers, rows) {
  return el("table", {},
    el("thead", {}, el("tr", {}, headers.map((header) => el("th", { text: header })))),
    el("tbody", {}, rows.map((row) => el("tr", {}, row.map((cell) =>
      cell instanceof Node ? el("td", {}, cell) : el("td", { text: cell === null || cell === undefined ? UI.none : String(cell) }))))));
}

function yesNo(value) {
  return value ? UI.yes : UI.no;
}

async function loadService() {
  let data;
  try {
    data = await api("/api/service");
  } catch (error) {
    status(error.message, true);
    return;
  }
  const c = UI.columns;
  const section = clear(document.getElementById("view-service"));
  section.append(el("div", { className: "toolbar" },
    el("strong", { text: UI.serviceTitle }),
    el("span", { className: "muted small", text: UI.refreshed(nowText()) }),
    el("span", { className: "spacer" }),
    el("button", { text: UI.refresh, onclick: () => loadService() })));
  const hb = data.heartbeat;
  const heartbeat = el("article", { className: "card" }, el("h2", { text: UI.heartbeat }));
  if (!hb) {
    heartbeat.append(el("div", { className: "muted", text: UI.noHeartbeat }));
  } else {
    heartbeat.append(el("div", { className: "meta" },
      el("span", { className: "badge " + (hb.ok ? "ok" : "bad"), text: String(hb.status) }),
      el("span", { className: "badge " + (hb.stale ? "bad" : "ok"), text: hb.stale ? UI.stale : UI.fresh }),
      el("span", { text: UI.age(age(hb.age_seconds)) })));
    const fields = ["ok", "pid", "version", "ts", "started_at", "last_event_at", "last_catchup_at", "gaps"];
    heartbeat.append(table([c.key, c.value], fields.map((key) => [key, typeof hb[key] === "boolean" ? yesNo(hb[key]) : hb[key]])));
    heartbeat.append(el("h2", { text: UI.counters }),
      table([c.key, c.value], Object.entries(hb.counters || {}).map(([key, value]) => [key, value])));
  }
  section.append(heartbeat);
  section.append(el("article", { className: "card" },
    el("h2", { text: UI.wake }),
    el("div", { className: "meta" },
      el("span", { className: "badge " + (data.wake_off ? "warn" : "ok"), text: data.wake_off ? UI.wakeOff : UI.wakeOn }),
      el("span", { text: UI.wakeConfig(data.wake_enabled_in_config) })),
    el("h2", { text: UI.humanQueue }),
    el("div", { className: "muted small", text: data.human_queue.mode + " / " + data.human_queue.digest_seconds + UI.units.s }),
    el("h2", { text: UI.adapters }),
    table([c.index, c.kind, c.purpose, c.dryRun], data.adapters.map((row) => [row.index, row.kind, row.purpose, yesNo(row.dry_run)]))));
  section.append(el("article", { className: "card" },
    el("h2", { text: UI.reconcile }),
    data.reconcile_passes.length
      ? table([c.at, c.counts], data.reconcile_passes.map((row) => [row.at,
        el("span", { className: "mono", text: Object.entries(row.counts).map(([key, value]) => key + "=" + value).join(" ") })]))
      : el("div", { className: "muted", text: UI.noReconcile }),
    el("h2", { text: UI.warnings }),
    data.warnings.length
      ? table([c.at, c.level, c.logger, c.message], data.warnings.map((row) => [row.at, row.level, row.logger,
        el("span", { className: "mono", text: row.message })]))
      : el("div", { className: "muted", text: UI.noWarnings }),
    el("h2", { text: UI.gaps }),
    data.gaps.length
      ? table([c.id, c.source, c.detected, c.lastEvent, c.resumed, c.catchup, c.note], data.gaps.map((row) =>
        [row.id, row.source, row.detected_at, row.last_event_at, row.resumed_at, row.catchup_at, row.note]))
      : el("div", { className: "muted", text: UI.noGaps })));
}

// ---------- scope proposals (P13) ----------

async function loadProposals() {
  let data;
  try {
    data = await api("/api/proposals");
  } catch (error) {
    status(error.message, true);
    return;
  }
  const c = UI.columns;
  const section = clear(document.getElementById("view-proposals"));
  section.append(el("div", { className: "toolbar" },
    el("strong", { text: UI.proposalsTitle(data.proposals.length, data.min_evidence) }),
    el("span", { className: "muted small", text: UI.refreshed(nowText()) }),
    el("span", { className: "spacer" }),
    el("button", { text: UI.refresh, onclick: () => loadProposals() })));
  const list = el("article", { className: "card" });
  if (!data.proposals.length) {
    list.append(el("div", { className: "muted", text: UI.proposalsEmpty }));
  } else {
    list.append(table([c.kind, c.scopeKey, c.name, c.lane, c.evidence, c.status, c.lanes, c.current,
      c.firstSeen, c.lastSeen, c.actions], data.proposals.map((row) => [
      row.kind, el("span", { className: "mono", text: row.key }), row.name, row.lane, row.evidence_count,
      el("span", {}, row.status,
        row.conflict ? el("span", { className: "badge bad", text: UI.conflict }) : null,
        row.effective ? el("span", { className: "badge ok", text: UI.effective }) : null,
        row.imported ? el("span", { className: "badge", text: UI.imported }) : null),
      row.lanes.map((item) => item.lane + " " + item.evidence_count).join(", "),
      row.current ? UI.current(row.current.lane === null ? UI.none : row.current.lane, row.current.source) : UI.none,
      row.first_seen, row.last_seen,
      el("span", {}, ["confirm", "reject", "defer"].map((action) =>
        el("button", { text: UI[action], onclick: () => proposalAction(action, row) })))])));
  }
  section.append(list);
  const learned = el("article", { className: "card" }, el("h2", { text: UI.learnedTitle }));
  if (!data.learned.length) {
    learned.append(el("div", { className: "muted", text: UI.learnedEmpty }));
  } else {
    learned.append(table([c.kind, c.scopeKey, c.name, c.lane, c.by, c.at, c.inConfig], data.learned.map((row) =>
      [row.kind, el("span", { className: "mono", text: row.key }), row.name, row.lane, row.confirmed_by,
        row.confirmed_at, yesNo(row.in_config)])));
  }
  const output = el("pre", { className: "mono", hidden: true });
  learned.append(el("div", { className: "toolbar" },
    el("button", { text: UI.exportButton, onclick: () => exportScope(output) }),
    el("span", { className: "muted small", text: UI.exportHint })), output);
  section.append(learned);
}

async function proposalAction(action, row) {
  let data;
  try {
    data = await api("/api/proposals/actions", { action, kind: row.kind, key: row.key, lane: row.lane });
  } catch (error) {
    status(error.message, true);
    return;
  }
  status(UI.proposalDone(data.status));
  loadProposals();
}

async function exportScope(output) {
  let data;
  try {
    data = await api("/api/proposals/export");
  } catch (error) {
    status(error.message, true);
    return;
  }
  output.textContent = data.yaml;
  output.hidden = false;
}

// ---------- search ----------

async function runSearch(query) {
  query = (query || "").trim();
  if (query.length < 2) return;
  let data;
  try {
    data = await api("/api/search?q=" + encodeURIComponent(query));
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (state.view !== "search") state.previousView = state.view;
  state.view = "search";
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== "view-search";
  const c = UI.columns;
  const section = clear(document.getElementById("view-search"));
  section.append(el("div", { className: "toolbar" },
    el("strong", { text: UI.searchTitle(data.query) }),
    el("span", { className: "spacer" }),
    el("button", { text: UI.close, onclick: () => showView(state.previousView, false) })));
  if (!data.rows.length) {
    section.append(el("div", { className: "empty-state", text: UI.searchEmpty }));
    return;
  }
  if (data.truncated) section.append(el("div", { className: "muted small", text: UI.searchTruncated }));
  section.append(el("article", { className: "card" }, table(
    [c.id, c.when, c.channel, c.author, c.state, c.lane, c.reason, c.note, c.text],
    data.rows.map((row) => [el("span", { className: "mono", text: row.id }), row.when, named(row.channel), named(row.author),
      row.state, row.lane, row.route_reason, row.note,
      row.snippet === null ? el("span", { className: "muted", text: UI.noBody }) : row.snippet + (row.truncated ? " " + UI.truncated : "")]))));
}

// ---------- start ----------

function applyStaticText() {
  document.title = UI.title;
  for (const node of document.querySelectorAll("[data-text]")) node.textContent = UI[node.dataset.text];
  for (const node of document.querySelectorAll("[data-placeholder]")) node.placeholder = UI[node.dataset.placeholder];
  for (const node of document.querySelectorAll("[data-aria]")) node.setAttribute("aria-label", UI[node.dataset.aria]);
}

function readHash() {
  const match = /^#msg=(.+)$/.exec(location.hash);
  if (match) state.pendingMsg = decodeURIComponent(match[1]);
}

async function start() {
  applyStaticText();
  readHash();
  try {
    state.config = await api("/api/config");
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (state.config.timezone_warning) status(state.config.timezone_warning, true);
  for (const link of document.querySelectorAll(".tabs a")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.dataset.view === "lanes") state.lane = null;
      showView(link.dataset.view, true);
    });
  }
  document.getElementById("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch(document.getElementById("search-input").value);
  });
  window.addEventListener("popstate", () => showView(VIEWS[location.pathname] || "queue", false));
  window.addEventListener("hashchange", () => {
    readHash();
    if (state.view === "queue") focusPendingMessage(); else showView("queue", true);
  });
  showView(VIEWS[location.pathname] || "queue", false);
  setInterval(() => { if (!document.hidden) refresh(); }, state.config.poll_seconds * 1000);
}

start();
