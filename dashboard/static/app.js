const lanesEl = document.getElementById("lanes");
const emptyState = document.getElementById("emptyState");
const connStatus = document.getElementById("connStatus");
const laneTemplate = document.getElementById("laneTemplate");
const newDashboardBtn = document.getElementById("newDashboard");

// One lane per user-created dashboard. Claude Desktop reuses a single MCP server
// process across every chat, so a dashboard -- not a chat -- is the unit of work.
const lanes = new Map();

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
    scriptInput: el.querySelector(".script-input"),
    runBtn: el.querySelector(".run-btn"),
    stopBtn: el.querySelector(".stop-btn"),
    runStatus: el.querySelector(".run-status"),
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
  lane.runBtn.addEventListener("click", () => runScript(lane));
  lane.stopBtn.addEventListener("click", () => stopScript(lane));

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
  lane.el.remove();
  lanes.delete(String(lane.id));
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

async function runScript(lane) {
  const script = lane.scriptInput.value;
  setRunStatus(lane, "starting…", "");
  const res = await fetch(`/dashboards/${encodeURIComponent(lane.id)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ script }),
  });
  const data = await res.json();
  if (!res.ok) {
    setRunStatus(lane, data.error || "failed", "err");
    return;
  }
  setRunStatus(lane, `queued ${data.queued} command(s)`, "");
}

async function stopScript(lane) {
  await fetch(`/dashboards/${encodeURIComponent(lane.id)}/stop`, { method: "POST" });
}

function setRunStatus(lane, text, cls) {
  lane.runStatus.textContent = text;
  lane.runStatus.className = `run-status ${cls}`;
}

function setRunning(lane, running) {
  lane.runBtn.disabled = running;
  lane.stopBtn.disabled = !running;
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

function handleEvent(event) {
  if (event.type === "dashboard_created") {
    if (!lanes.has(String(event.dashboard))) {
      createLane({ id: event.dashboard, name: event.name, file: null });
    }
    return;
  }
  if (event.type === "dashboard_deleted") {
    const lane = lanes.get(String(event.dashboard));
    if (lane) {
      lane.el.remove();
      lanes.delete(String(event.dashboard));
      if (lanes.size === 0) emptyState.hidden = false;
    }
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
    const node = lane.nodes[event.tool] || lane.nodes.idle;
    clearNodeStates(lane);
    node.classList.add("active");
    lane.avatar.classList.add("active");
    moveAvatarTo(lane, node);
    addLogEntry(lane, event, "started");
  } else if (event.type === "tool_call_finished") {
    const node = lane.nodes[event.tool] || lane.nodes.idle;
    const ok = !event.error;
    node.classList.remove("active");
    node.classList.add(ok ? "success" : "error");
    lane.avatar.classList.remove("active");
    lane.avatar.classList.add(ok ? "success" : "error");
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
window.addEventListener("load", () => {
  loadDashboards();
  connect();
});
