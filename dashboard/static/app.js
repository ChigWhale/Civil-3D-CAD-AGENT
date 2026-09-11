const lanesEl = document.getElementById("lanes");
const emptyState = document.getElementById("emptyState");
const connStatus = document.getElementById("connStatus");
const laneTemplate = document.getElementById("laneTemplate");
const blockTemplate = document.getElementById("blockTemplate");
const newDashboardBtn = document.getElementById("newDashboard");

// One lane per user-created dashboard. Claude Desktop reuses a single MCP server
// process across every chat, so a dashboard -- not a chat -- is the unit of work.
const lanes = new Map();

// Filled from GET /blocks before any lane is built: every palette button and
// every parameter input is generated from these specs, so a block added to
// blocks.py shows up here without touching this file.
let blockSpecs = [];
let specsByType = new Map();
let maxRepeat = 500;

// How long a finished tool stays in the bubble before the agent goes back to idle.
const BUBBLE_HOLD_MS = 1200;
// How many recent tools the trail under the agent keeps.
const TRAIL_LEN = 8;

// Standard AutoCAD Color Index entries worth previewing. Anything outside this
// set just gets no swatch rather than a wrong one.
const ACI_SWATCHES = {
  0: "#8b93a6", 1: "#ff0000", 2: "#ffff00", 3: "#00ff00", 4: "#00ffff",
  5: "#0000ff", 6: "#ff00ff", 7: "#ffffff", 8: "#808080", 9: "#c0c0c0",
  250: "#333333", 251: "#505050", 252: "#696969", 253: "#828282",
  254: "#bebebe", 255: "#ffffff", 256: "#8b93a6",
};

let uidCounter = 0;
const nextUid = () => `b${++uidCounter}`;

// ── Lanes ─────────────────────────────────────────────────────────────────────

function createLane(board) {
  const el = laneTemplate.content.firstElementChild.cloneNode(true);
  el.querySelector(".lane-name").textContent = board.name;
  el.querySelector(".lane-id").textContent = `id ${board.id}`;

  const avatarUrl = `/avatar?dashboard=${encodeURIComponent(board.id)}&ts=${Date.now()}`;
  const avatar = el.querySelector(".avatar");
  const thumb = el.querySelector(".avatar-thumb");
  avatar.src = avatarUrl;
  thumb.src = avatarUrl;

  const lane = {
    id: board.id,
    el,
    avatar,
    thumb,
    bubble: el.querySelector(".tool-bubble"),
    bubbleName: el.querySelector(".tool-bubble-name"),
    bubbleDetail: el.querySelector(".tool-bubble-detail"),
    trail: el.querySelector(".tool-trail"),
    log: el.querySelector(".log"),
    fileInput: el.querySelector(".file-input"),
    fileStatus: el.querySelector(".file-status"),
    palette: el.querySelector(".palette"),
    paletteSearch: el.querySelector(".palette-search"),
    paletteMenu: el.querySelector(".palette-menu"),
    paletteCount: el.querySelector(".palette-count"),
    sequenceEl: el.querySelector(".sequence"),
    sequenceEmpty: el.querySelector(".sequence-empty"),
    runBtn: el.querySelector(".run-btn"),
    resumeBtn: el.querySelector(".resume-btn"),
    stopBtn: el.querySelector(".stop-btn"),
    clearBtn: el.querySelector(".clear-btn"),
    runStatus: el.querySelector(".run-status"),
    blocks: loadSequence(board.id), // [{uid, type, params, repeat}]
    returnTimer: null,
    runToken: 0,
  };

  if (board.file) lane.fileInput.value = board.file;

  el.querySelector(".file-set").addEventListener("click", () => assignFile(lane));
  lane.fileInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") assignFile(lane);
  });
  el.querySelector(".avatar-input").addEventListener("change", (e) =>
    uploadAvatar(lane, e.target.files[0])
  );
  el.querySelector(".delete-btn").addEventListener("click", () => deleteLane(lane));
  lane.runBtn.addEventListener("click", () => runSequence(lane));
  lane.resumeBtn.addEventListener("click", () => resumeSequence(lane));
  lane.stopBtn.addEventListener("click", () => stopSequence(lane));
  lane.clearBtn.addEventListener("click", () => {
    lane.blocks = [];
    renderSequence(lane);
  });

  buildPalette(lane);
  wireDragAndDrop(lane);
  renderSequence(lane);

  lanesEl.appendChild(el);
  lanes.set(String(board.id), lane);
  emptyState.hidden = true;
  return lane;
}

async function createDashboard() {
  const res = await fetch("/dashboards", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  const board = await res.json();
  if (!lanes.has(String(board.id))) createLane(board);
}

async function deleteLane(lane) {
  await fetch(`/dashboards/${encodeURIComponent(lane.id)}`, { method: "DELETE" });
  removeLane(lane.id);
}

function removeLane(id) {
  const lane = lanes.get(String(id));
  if (!lane) return;
  lane.el.remove();
  lanes.delete(String(id));
  if (lanes.size === 0) emptyState.hidden = false;
}

async function assignFile(lane) {
  const file = lane.fileInput.value.trim();
  const res = await fetch(`/dashboards/${encodeURIComponent(lane.id)}/file`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file }),
  });
  const data = await res.json();
  if (!res.ok) {
    lane.fileStatus.textContent = data.error || "failed";
    lane.fileStatus.className = "file-status err";
    return;
  }
  lane.fileStatus.textContent = data.file ? "assigned" : "cleared";
  lane.fileStatus.className = "file-status ok";

  // Alignment names come from the drawing, so a different drawing means a
  // different list: drop the cache and redraw the forms against the new one.
  forgetOptions(lane);
  renderSequence(lane);
}

// ── Block palette and sequence ────────────────────────────────────────────────

/** A search box over a grouped menu. With 100+ blocks a row of buttons would be
 *  most of the screen, so the menu only opens on focus and narrows as you type;
 *  Enter adds the first match. */
function buildPalette(lane) {
  const menu = lane.paletteMenu;
  const search = lane.paletteSearch;
  const groups = new Map(); // group name -> [{spec, button}]

  for (const spec of blockSpecs) {
    const group = spec.group || "Other";
    if (!groups.has(group)) groups.set(group, []);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "palette-btn";
    btn.innerHTML = `<span class="palette-btn-label"></span><span class="palette-btn-desc"></span>`;
    btn.querySelector(".palette-btn-label").textContent = spec.label;
    btn.querySelector(".palette-btn-desc").textContent = spec.description;
    btn.title = `${spec.help || spec.description}\n\ntyped form: ${spec.usage}`;
    // mousedown, not click: the search box blurs on click and would close the menu first.
    btn.addEventListener("mousedown", (e) => {
      e.preventDefault();
      addBlock(lane, spec.type);
      search.value = "";
      filterPalette(lane, "");
      closePalette(lane);
    });
    groups.get(group).push({ spec, button: btn });
  }

  lane.paletteGroups = [];
  for (const [name, items] of groups) {
    const section = document.createElement("div");
    section.className = "palette-section";
    const head = document.createElement("div");
    head.className = "palette-group";
    head.textContent = name;
    section.appendChild(head);
    const list = document.createElement("div");
    list.className = "palette-list";
    for (const { button } of items) list.appendChild(button);
    section.appendChild(list);
    menu.appendChild(section);
    lane.paletteGroups.push({ section, items });
  }
  lane.paletteCount.textContent = `${blockSpecs.length} commands`;

  search.addEventListener("focus", () => openPalette(lane));
  search.addEventListener("input", () => {
    openPalette(lane);
    filterPalette(lane, search.value);
  });
  search.addEventListener("blur", () => closePalette(lane));
  search.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      search.value = "";
      filterPalette(lane, "");
      search.blur();
    } else if (e.key === "Enter") {
      const first = menu.querySelector(".palette-btn:not([hidden])");
      if (first) {
        e.preventDefault();
        first.dispatchEvent(new MouseEvent("mousedown", { cancelable: true }));
      }
    }
  });
}

function openPalette(lane) {
  lane.paletteMenu.hidden = false;
}

function closePalette(lane) {
  lane.paletteMenu.hidden = true;
}

function filterPalette(lane, query) {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  let shown = 0;
  for (const { section, items } of lane.paletteGroups) {
    let visible = 0;
    for (const { spec, button } of items) {
      const hay = `${spec.type} ${spec.label} ${spec.description} ${spec.group}`.toLowerCase();
      const match = terms.every((t) => hay.includes(t));
      button.hidden = !match;
      if (match) visible++;
    }
    section.hidden = visible === 0;
    shown += visible;
  }
  lane.paletteCount.textContent = terms.length
    ? `${shown} of ${blockSpecs.length} commands`
    : `${blockSpecs.length} commands`;
}

/** A block's starting parameters: whatever the spec calls a default, as a
 *  string, since the server coerces every value it is given. */
function defaultParams(spec) {
  const params = {};
  for (const field of spec.fields) {
    params[field.name] = field.default === undefined ? "" : String(field.default);
  }
  return params;
}

function addBlock(lane, type, atIndex) {
  const spec = specsByType.get(type);
  if (!spec) return;
  const block = { uid: nextUid(), type, params: defaultParams(spec), repeat: 1 };
  lane.blocks.splice(atIndex === undefined ? lane.blocks.length : atIndex, 0, block);
  renderSequence(lane);
  const card = lane.sequenceEl.querySelector(`[data-uid="${block.uid}"] input, ` +
                                             `[data-uid="${block.uid}"] select`);
  if (card) card.focus();
}

function moveBlock(lane, uid, delta) {
  const from = lane.blocks.findIndex((b) => b.uid === uid);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= lane.blocks.length) return;
  const [block] = lane.blocks.splice(from, 1);
  lane.blocks.splice(to, 0, block);
  renderSequence(lane);
}

function duplicateBlock(lane, uid) {
  const index = lane.blocks.findIndex((b) => b.uid === uid);
  if (index < 0) return;
  const source = lane.blocks[index];
  lane.blocks.splice(index + 1, 0, {
    uid: nextUid(),
    type: source.type,
    params: { ...source.params },
    repeat: source.repeat,
  });
  renderSequence(lane);
}

function removeBlock(lane, uid) {
  lane.blocks = lane.blocks.filter((b) => b.uid !== uid);
  renderSequence(lane);
}

/** Rebuilds the whole list. Only called for structural changes (add, remove,
 *  reorder, duplicate) -- typing in a field writes straight to the model, so an
 *  input never loses focus mid-edit. */
function renderSequence(lane) {
  lane.sequenceEl.textContent = "";
  lane.blocks.forEach((block, index) => {
    lane.sequenceEl.appendChild(renderBlock(lane, block, index));
  });
  lane.sequenceEmpty.hidden = lane.blocks.length > 0;
  lane.runBtn.disabled = lane.blocks.length === 0 || !lane.stopBtn.disabled;
  saveSequence(lane);
}

function renderBlock(lane, block, index) {
  const spec = specsByType.get(block.type);
  const el = blockTemplate.content.firstElementChild.cloneNode(true);
  el.dataset.uid = block.uid;
  el.querySelector(".block-step").textContent = index + 1;
  el.querySelector(".block-label").textContent = spec ? spec.label : block.type;
  el.querySelector(".block-desc").textContent = spec ? spec.description : "unknown block";
  if (spec && spec.help) el.querySelector(".block-desc").title = spec.help;

  const repeat = el.querySelector(".repeat-input");
  repeat.max = maxRepeat;
  repeat.value = block.repeat;
  repeat.addEventListener("input", () => {
    block.repeat = repeat.value === "" ? 1 : Number(repeat.value);
    saveSequence(lane);
  });

  el.querySelector(".block-up").addEventListener("click", () => moveBlock(lane, block.uid, -1));
  el.querySelector(".block-down").addEventListener("click", () => moveBlock(lane, block.uid, 1));
  el.querySelector(".block-dup").addEventListener("click", () => duplicateBlock(lane, block.uid));
  el.querySelector(".block-del").addEventListener("click", () => removeBlock(lane, block.uid));

  const params = el.querySelector(".block-params");
  if (spec) {
    for (const field of spec.fields) {
      params.appendChild(renderField(lane, block, field));
    }
  }
  return el;
}

function renderField(lane, block, field) {
  const wrap = document.createElement("label");
  wrap.className = `param param-${field.type}`;

  const name = document.createElement("span");
  name.className = "param-name";
  name.textContent = field.label;
  if (field.help) wrap.title = field.help;
  wrap.appendChild(name);

  const value = block.params[field.name] ?? "";
  const optionalBlank = field.default === undefined;
  const swatch = field.type === "color" ? document.createElement("span") : null;
  let input;

  if (field.type === "multichoice") {
    input = document.createElement("select");
    input.multiple = true;
    input.size = 4;
    const chosen = new Set(Array.isArray(value) ? value.map(String) : []);

    /* Options come from the assigned drawing, not the block spec, so the list
       is fetched per lane and cached. Until it arrives the select shows why it
       is empty rather than looking broken. */
    const placeholder = document.createElement("option");
    placeholder.disabled = true;
    placeholder.textContent = "loading\u2026";
    input.appendChild(placeholder);

    optionsFor(lane, field.options_from).then(({ names, why }) => {
      input.textContent = "";
      if (!names.length) {
        const empty = document.createElement("option");
        empty.disabled = true;
        empty.textContent = why ? `none \u2014 ${why}` : "none in this drawing";
        input.appendChild(empty);
        return;
      }
      for (const name of names) {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        option.selected = chosen.has(name);
        input.appendChild(option);
      }
    });

    input.className = "param-input";
    input.addEventListener("change", () => {
      block.params[field.name] = Array.from(input.selectedOptions).map((o) => o.value);
      saveSequence(lane);
    });
    wrap.appendChild(input);

    const hint = document.createElement("span");
    hint.className = "param-hint";
    hint.textContent = "none = all";
    wrap.appendChild(hint);
    return wrap;
  }

  if (field.type === "choice" || field.type === "bool") {
    input = document.createElement("select");
    const options =
      field.type === "bool"
        ? [["true", "yes"], ["false", "no"]]
        : field.options.map((o) => [o, o]);
    if (optionalBlank) options.unshift(["", "unchanged"]);
    for (const [optValue, optLabel] of options) {
      const option = document.createElement("option");
      option.value = optValue;
      option.textContent = optLabel;
      input.appendChild(option);
    }
    input.value = String(value).toLowerCase() === "true" ? "true"
      : String(value).toLowerCase() === "false" ? "false"
      : value;
  } else if (field.type === "points") {
    input = document.createElement("textarea");
    input.rows = 2;
    input.placeholder = "0,0; 10,0; 10,10";
    input.value = value;
  } else if (field.type === "handles") {
    input = document.createElement("input");
    input.type = "text";
    input.placeholder = "2A1, 2A2";
    input.value = value;
  } else if (field.type === "point") {
    input = document.createElement("input");
    input.type = "text";
    input.placeholder = optionalBlank ? "x,y (optional)" : "x,y";
    input.value = value;
  } else if (field.type === "strings") {
    input = document.createElement("input");
    input.type = "text";
    input.placeholder = optionalBlank ? "a, b (optional)" : "a, b";
    input.value = value;
  } else if (field.type === "json") {
    input = document.createElement("textarea");
    input.rows = 2;
    input.placeholder = optionalBlank ? '{"TAG": "value"} (optional)' : "JSON";
    input.value = value;
  } else if (field.type === "number" || field.type === "int" || field.type === "color") {
    input = document.createElement("input");
    input.type = "number";
    input.step = field.type === "number" ? "any" : "1";
    if (field.min !== undefined) input.min = field.min;
    if (field.max !== undefined) input.max = field.max;
    if (optionalBlank) input.placeholder = "unchanged";
    input.value = value;
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.value = value;
  }

  input.className = "param-input";
  input.addEventListener("input", () => {
    block.params[field.name] = input.value;
    if (swatch) paintSwatch(swatch, input.value);
    saveSequence(lane);
  });

  if (swatch) {
    swatch.className = "param-swatch";
    paintSwatch(swatch, value);
    wrap.appendChild(swatch);
  }

  wrap.appendChild(input);
  return wrap;
}

/** Per-lane cache of drawing-sourced option lists (currently alignments).
 *  Refetched when the lane's drawing changes, since the names come from it. */
const optionCache = new Map();

function optionsCacheKey(lane, source) {
  return `${lane.id}:${source}:${lane.fileInput.value.trim()}`;
}

async function optionsFor(lane, source) {
  const key = optionsCacheKey(lane, source);
  if (optionCache.has(key)) return optionCache.get(key);

  const pending = (async () => {
    try {
      const res = await fetch(
        `/dashboards/${encodeURIComponent(lane.id)}/${source}`);
      const body = await res.json();
      return { names: body[source] || [], why: body.why || "" };
    } catch (e) {
      return { names: [], why: "the dashboard server did not answer" };
    }
  })();

  optionCache.set(key, pending);
  return pending;
}

/** The drawing changed, so anything sourced from it is stale. */
function forgetOptions(lane) {
  for (const key of Array.from(optionCache.keys())) {
    if (key.startsWith(`${lane.id}:`)) optionCache.delete(key);
  }
}

function paintSwatch(swatch, value) {
  const color = ACI_SWATCHES[Number(value)];
  swatch.style.background = color || "transparent";
  swatch.style.visibility = color ? "visible" : "hidden";
}

/** Drag-to-reorder. The card moves in the DOM during the drag, then the DOM
 *  order is read back into the model on drop -- so a re-render (which would kill
 *  the drag) never happens mid-gesture. */
function wireDragAndDrop(lane) {
  const list = lane.sequenceEl;

  list.addEventListener("dragstart", (e) => {
    const card = e.target.closest(".block");
    if (!card) return;
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", card.dataset.uid);
  });

  list.addEventListener("dragover", (e) => {
    const dragging = list.querySelector(".dragging");
    if (!dragging) return;
    e.preventDefault();
    const next = [...list.querySelectorAll(".block:not(.dragging)")].find((card) => {
      const box = card.getBoundingClientRect();
      return e.clientY < box.top + box.height / 2;
    });
    if (next) list.insertBefore(dragging, next);
    else list.appendChild(dragging);
  });

  list.addEventListener("dragend", () => {
    const dragging = list.querySelector(".dragging");
    if (dragging) dragging.classList.remove("dragging");
    const order = [...list.children].map((card) => card.dataset.uid);
    lane.blocks.sort((a, b) => order.indexOf(a.uid) - order.indexOf(b.uid));
    renderSequence(lane);
  });
}

// ── Persistence ───────────────────────────────────────────────────────────────
//
// Sequences live in the browser: the server holds dashboards and runs, not
// half-built sequences, so a reload shouldn't cost the user their blocks.

const storageKey = (id) => `cad-dashboard-blocks:${id}`;

function saveSequence(lane) {
  try {
    localStorage.setItem(storageKey(lane.id), JSON.stringify(lane.blocks));
  } catch (e) {
    /* private mode or a full quota — the sequence just won't survive a reload */
  }
}

function loadSequence(id) {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey(id)) || "[]");
    if (!Array.isArray(saved)) return [];
    return saved
      .filter((b) => b && specsByType.has(b.type))
      .map((b) => ({
        uid: nextUid(),
        type: b.type,
        params: { ...defaultParams(specsByType.get(b.type)), ...(b.params || {}) },
        repeat: Number(b.repeat) || 1,
      }));
  } catch (e) {
    return [];
  }
}

// ── Running ───────────────────────────────────────────────────────────────────

async function runSequence(lane) {
  if (!lane.blocks.length) {
    setRunStatus(lane, "add a block first", "err");
    return;
  }
  clearBlockStates(lane);
  setRunStatus(lane, "starting…", "");

  const res = await fetch(`/dashboards/${encodeURIComponent(lane.id)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      blocks: lane.blocks.map((b) => ({
        id: b.uid,
        type: b.type,
        params: b.params,
        repeat: b.repeat,
      })),
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    setRunStatus(lane, data.error || "failed", "err");
    flagBlockFromError(lane, data.error);
    return;
  }
  setRunStatus(lane, `queued ${data.queued} command(s)`, "");
}

async function stopSequence(lane) {
  await fetch(`/dashboards/${encodeURIComponent(lane.id)}/stop`, { method: "POST" });
}

/** A run paused on conflicts (policy "pause") waits for this, or for a resume
 *  from the chat. The reason is recorded with the decision. */
async function resumeSequence(lane) {
  const reason = window.prompt("Resume the paused run? Say why (recorded with the decision):", "reviewed in the browser");
  if (reason === null) return;
  await fetch(`/dashboards/${encodeURIComponent(lane.id)}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason, by: "browser" }),
  });
}

function setPaused(lane, paused) {
  lane.resumeBtn.hidden = !paused;
  lane.resumeBtn.disabled = !paused;
}

/** Server-side validation errors are prefixed "block N (type): ..." — mark that
 *  card so the user doesn't have to count down the list. */
function flagBlockFromError(lane, message) {
  const match = /^block (\d+)\b/.exec(message || "");
  if (!match) return;
  const card = lane.sequenceEl.children[Number(match[1]) - 1];
  if (card) card.classList.add("invalid");
}

function blockCard(lane, uid) {
  return uid ? lane.sequenceEl.querySelector(`[data-uid="${uid}"]`) : null;
}

function clearBlockStates(lane) {
  lane.sequenceEl.querySelectorAll(".block").forEach((card) =>
    card.classList.remove("running", "ok", "err", "invalid")
  );
}

function setRunStatus(lane, text, cls) {
  lane.runStatus.textContent = text;
  lane.runStatus.className = `run-status ${cls}`;
}

function setRunning(lane, running) {
  lane.stopBtn.disabled = !running;
  lane.runBtn.disabled = running || lane.blocks.length === 0;
  lane.clearBtn.disabled = running;
  if (!running) setPaused(lane, false);
}

async function uploadAvatar(lane, file) {
  if (!file) return;
  const formData = new FormData();
  formData.append("file", file);
  await fetch(`/avatar?dashboard=${encodeURIComponent(lane.id)}`, {
    method: "POST",
    body: formData,
  });
  const url = `/avatar?dashboard=${encodeURIComponent(lane.id)}&ts=${Date.now()}`;
  lane.avatar.src = url;
  lane.thumb.src = url;
}

// ── Stage: the agent stays put and the tool it is using appears above it ────

const STATES = ["idle", "active", "success", "error"];

/** Puts a tool name (and a one-line detail) in the bubble over the avatar. */
function showTool(lane, tool, state, detail) {
  lane.bubble.classList.remove(...STATES, "pop");
  lane.avatar.classList.remove("active", "success", "error");
  lane.bubbleName.textContent = tool;
  lane.bubbleDetail.textContent = detail || "";
  lane.bubbleDetail.hidden = !detail;
  lane.bubble.classList.add(state);
  if (state !== "idle") {
    lane.avatar.classList.add(state);
    // Restart the pop animation even when the same tool fires twice in a row.
    void lane.bubble.offsetWidth;
    lane.bubble.classList.add("pop");
  }
}

function setIdle(lane) {
  showTool(lane, "idle", "idle", "");
}

/** Adds a chip for a finished call under the avatar, newest first. */
function pushTrail(lane, tool, ok, durationMs) {
  const chip = document.createElement("span");
  chip.className = `trail-chip ${ok ? "ok" : "err"}`;
  chip.textContent = tool;
  chip.title = ok ? `${durationMs} ms` : "failed";
  lane.trail.prepend(chip);
  while (lane.trail.children.length > TRAIL_LEN) lane.trail.lastElementChild.remove();
}

/** A short, readable preview of a call's arguments for the bubble. */
function argsPreview(args) {
  if (!args || typeof args !== "object") return "";
  const parts = [];
  for (const [key, value] of Object.entries(args)) {
    if (key === "dashboard") continue;
    let text = typeof value === "string" ? value : JSON.stringify(value);
    if (text.length > 24) text = text.slice(0, 23) + "…";
    parts.push(`${key}=${text}`);
    if (parts.length === 3) break;
  }
  return parts.join("  ");
}

/** The first thing a result says, if it is the JSON the plugin returns. */
function resultPreview(result) {
  if (!result) return "";
  try {
    const parsed = JSON.parse(result);
    if (parsed && typeof parsed === "object") {
      const created = Array.isArray(parsed.created) ? parsed.created.length : 0;
      const msg = parsed.message || parsed.error || "";
      return created ? `${msg} → ${created} handle(s)` : msg;
    }
  } catch (e) {
    /* plain text result */
  }
  return String(result).slice(0, 80);
}

function handleEvent(event) {
  if (event.type === "dashboard_created") {
    if (!lanes.has(String(event.dashboard))) {
      createLane({ id: event.dashboard, name: event.name, file: null });
    }
    return;
  }
  if (event.type === "dashboard_deleted") {
    removeLane(event.dashboard);
    return;
  }
  if (event.type === "dashboard_file_set") {
    const lane = lanes.get(String(event.dashboard));
    if (lane && document.activeElement !== lane.fileInput) lane.fileInput.value = event.file;
    return;
  }

  const lane = lanes.get(String(event.dashboard));
  if (!lane) return; // activity for a dashboard this page doesn't know about

  if (event.type === "run_started") {
    setRunning(lane, true);
    setPaused(lane, false);
    setRunStatus(lane, `running 0/${event.total}`, "");
    lane.runTotal = event.total;
    lane.runDone = 0;
    return;
  }
  if (event.type === "run_paused") {
    // The pre-run check found conflicts and the run's policy says wait. The
    // Stop button already works; Resume lets it go ahead with a reason.
    setRunning(lane, true);
    setPaused(lane, true);
    setRunStatus(lane, `paused — ${event.message}`, "err");
    addLogEntry(lane, { tool: "gate", _ts: event._ts, warning: event.message }, "warning");
    return;
  }
  if (event.type === "attention") {
    addLogEntry(lane, { tool: event.kind, _ts: event._ts, warning: `needs a decision: ${event.message}` }, "warning");
    return;
  }
  if (event.type === "run_finished") {
    setRunning(lane, false);
    setRunStatus(lane, `done — ${event.completed} command(s)`, "ok");
    return;
  }
  if (event.type === "run_stopped") {
    setRunning(lane, false);
    setRunStatus(lane, `stopped after ${event.completed}`, "");
    return;
  }
  if (event.type === "run_failed") {
    setRunning(lane, false);
    setRunStatus(lane, `failed after ${event.completed}: ${event.error}`, "err");
    return;
  }
  if (event.type === "run_warning") {
    // The dry-run gate: a generated block checked its own output against the
    // drawing before posting. A crossing is worth a look, not a refusal, so it
    // lands in the log and the run carries on.
    addLogEntry(lane, { tool: event.tool, _ts: event._ts, warning: event.message }, "warning");
    return;
  }
  if (event.type === "run_verified") {
    // The post-run check: the spatial index rebuilt and compared with the one
    // from before the run. A change outside the planned area is worth a look.
    const detail = (event.unexpected || []).slice(0, 3).map(u => u.after).join(" | ");
    addLogEntry(lane, { tool: "verify", _ts: event._ts,
                        warning: event.message + (detail ? ` — ${detail}` : "") },
                event.ok ? "ok" : "warning");
    return;
  }

  if (event.type === "tool_call_started") {
    clearTimeout(lane.returnTimer);
    lane.runToken += 1;
    showTool(lane, event.tool, "active", argsPreview(event.args));

    const card = blockCard(lane, event.block);
    if (card) {
      card.classList.remove("ok", "err", "invalid");
      card.classList.add("running");
    }
    addLogEntry(lane, event, "started");
  } else if (event.type === "tool_call_finished") {
    const ok = !event.error;
    showTool(lane, event.tool, ok ? "success" : "error",
             ok ? resultPreview(event.result) : String(event.error).slice(0, 80));
    pushTrail(lane, event.tool, ok, event.duration_ms);

    const card = blockCard(lane, event.block);
    if (card) {
      card.classList.remove("running");
      card.classList.add(ok ? "ok" : "err");
    }
    addLogEntry(lane, event, "finished");

    if (lane.runTotal) {
      lane.runDone = (lane.runDone || 0) + 1;
      setRunStatus(lane, `running ${lane.runDone}/${lane.runTotal}`, "");
    }

    const myToken = lane.runToken;
    lane.returnTimer = setTimeout(() => {
      if (myToken !== lane.runToken) return; // a newer call started meanwhile
      setIdle(lane);
    }, BUBBLE_HOLD_MS);
  }
}

function addLogEntry(lane, event, phase) {
  const el = document.createElement("div");
  el.className = `log-entry ${phase} ${event.error ? "error" : ""}`;
  const time = new Date((event._ts || Date.now() / 1000) * 1000).toLocaleTimeString();
  const detail =
    phase === "started"
      ? JSON.stringify(event.args || {})
      : phase === "warning"
      ? `WARNING: ${event.warning}`
      : event.error
      ? `ERROR: ${event.error}`
      : `${event.result || ""} (${event.duration_ms}ms)`;
  el.innerHTML = `<span class="t">${time}</span><span class="tool">${event.tool}</span> ${phase} ${detail}`;
  lane.log.appendChild(el);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    connStatus.textContent = "live";
    connStatus.className = "conn live";
  };
  ws.onclose = () => {
    connStatus.textContent = "disconnected — retrying…";
    connStatus.className = "conn down";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (msg) => {
    try {
      handleEvent(JSON.parse(msg.data));
    } catch (e) {
      console.error("bad event", e);
    }
  };
}

async function loadBlockSpecs() {
  try {
    const data = await (await fetch("/blocks")).json();
    blockSpecs = data.blocks || [];
    specsByType = new Map(blockSpecs.map((spec) => [spec.type, spec]));
    if (data.max_repeat) maxRepeat = data.max_repeat;
  } catch (e) {
    console.error("could not load block specs", e);
  }
}

async function loadDashboards() {
  try {
    const { dashboards } = await (await fetch("/dashboards")).json();
    for (const b of dashboards) if (!lanes.has(String(b.id))) createLane(b);
  } catch (e) {
    console.error("could not load dashboards", e);
  }
}

newDashboardBtn.addEventListener("click", createDashboard);
window.addEventListener("load", async () => {
  // Specs first: a lane can't build its palette or restore a saved sequence
  // without them.
  await loadBlockSpecs();
  loadDashboards();
  connect();
});
