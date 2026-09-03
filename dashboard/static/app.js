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

// The board draws the agent's MCP tools; a sequence sends raw plugin intents.
// These are the two names for the same square.
const NODE_ALIASES = {
  add_to_layer: "move_to_layer",
  remove_from_layer: "move_to_layer",
};

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

  const nodes = {};
  el.querySelectorAll(".node").forEach((n) => (nodes[n.dataset.tool] = n));

  const lane = {
    id: board.id,
    el,
    avatar,
    thumb,
    nodes,
    board: el.querySelector(".board"),
    log: el.querySelector(".log"),
    fileInput: el.querySelector(".file-input"),
    fileStatus: el.querySelector(".file-status"),
    palette: el.querySelector(".palette"),
    sequenceEl: el.querySelector(".sequence"),
    sequenceEmpty: el.querySelector(".sequence-empty"),
    runBtn: el.querySelector(".run-btn"),
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

  requestAnimationFrame(() => moveAvatarTo(lane, lane.nodes.idle));
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
}

// ── Block palette and sequence ────────────────────────────────────────────────

function buildPalette(lane) {
  let group = null;
  for (const spec of blockSpecs) {
    if (spec.group && spec.group !== group) {
      group = spec.group;
      const label = document.createElement("span");
      label.className = "palette-group";
      label.textContent = group;
      lane.palette.appendChild(label);
    }
    const btn = document.createElement("button");
    btn.className = "palette-btn";
    btn.textContent = spec.label;
    btn.title = `${spec.description}\n\ntyped form: ${spec.usage}`;
    btn.addEventListener("click", () => addBlock(lane, spec.type));
    lane.palette.appendChild(btn);
  }
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

function moveAvatarTo(lane, nodeEl) {
  if (!nodeEl) return;
  const boardRect = lane.board.getBoundingClientRect();
  const nodeRect = nodeEl.getBoundingClientRect();
  const x = nodeRect.left - boardRect.left + nodeRect.width / 2 - lane.avatar.offsetWidth / 2;
  const y = nodeRect.top - boardRect.top + nodeRect.height / 2 - lane.avatar.offsetHeight / 2;
  lane.avatar.style.transform = `translate(${x}px, ${y}px)`;
}

function clearNodeStates(lane) {
  Object.values(lane.nodes).forEach((n) => n.classList.remove("active", "success", "error"));
  lane.avatar.classList.remove("active", "success", "error");
}

function nodeFor(lane, tool) {
  return lane.nodes[tool] || lane.nodes[NODE_ALIASES[tool]] || lane.nodes.idle;
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
    setRunStatus(lane, `running 0/${event.total}`, "");
    lane.runTotal = event.total;
    lane.runDone = 0;
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

  if (event.type === "tool_call_started") {
    clearTimeout(lane.returnTimer);
    lane.runToken += 1;
    const node = nodeFor(lane, event.tool);
    clearNodeStates(lane);
    node.classList.add("active");
    lane.avatar.classList.add("active");
    moveAvatarTo(lane, node);

    const card = blockCard(lane, event.block);
    if (card) {
      card.classList.remove("ok", "err", "invalid");
      card.classList.add("running");
    }
    addLogEntry(lane, event, "started");
  } else if (event.type === "tool_call_finished") {
    const node = nodeFor(lane, event.tool);
    const ok = !event.error;
    node.classList.remove("active");
    node.classList.add(ok ? "success" : "error");
    lane.avatar.classList.remove("active");
    lane.avatar.classList.add(ok ? "success" : "error");

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
      clearNodeStates(lane);
      moveAvatarTo(lane, lane.nodes.idle);
    }, 1000);
  }
}

function addLogEntry(lane, event, phase) {
  const el = document.createElement("div");
  el.className = `log-entry ${phase} ${event.error ? "error" : ""}`;
  const time = new Date((event._ts || Date.now() / 1000) * 1000).toLocaleTimeString();
  const detail =
    phase === "started"
      ? JSON.stringify(event.args || {})
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
window.addEventListener("resize", () =>
  lanes.forEach((lane) => moveAvatarTo(lane, lane.nodes.idle))
);
window.addEventListener("load", async () => {
  // Specs first: a lane can't build its palette or restore a saved sequence
  // without them.
  await loadBlockSpecs();
  loadDashboards();
  connect();
});
