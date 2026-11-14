'use strict';

// 单页控制台：Pong 画面、UP / DOWN 指示灯、网络控制和节点图。

const GAP = 16;          // 节点间距（世界坐标）
const RADIUS = 5;
const CLUSTER_COLS = 40; // 800 个集群成员排成 40×20
const OTHERS_COLS = 120;
const CLUSTER_NAMES = ['up', 'down', 'sense_up', 'sense_down'];
const CLUSTER_LABELS = { up: '操作 UP', down: '操作 DOWN', sense_up: '感觉 UP', sense_down: '感觉 DOWN' };

const state = {
  meta: null,
  assignment: new Map(),   // id -> { region, index }
  positions: new Map(),    // id -> { x, y }
  others: [],
  regions: {},
  nodeData: new Map(),     // id -> { active, value }
  edgeCount: 0,            // 当前连接总数（增量帧时局部加减）
  outEdges: new Map(),     // id -> [{ dst, w }]
  view: { x: 0, y: 0, scale: 1 },
  hover: null,
  pinned: null,
  fitPending: true,
};

const graphCanvas = document.getElementById('graph');
const graphCtx = graphCanvas.getContext('2d');
const pongCanvas = document.getElementById('pong');
const pongCtx = pongCanvas.getContext('2d');
let dpr = 1;
let width = 0;
let height = 0;
let renderQueued = false;

/* ---------- 布局 ---------- */

function buildLayout(meta) {
  state.meta = meta;
  state.assignment = new Map();
  state.positions = new Map();
  state.others = [];
  for (let id = 0; id < meta.visual_size; id += 1) {
    state.assignment.set(id, { region: 'visual', index: id });
  }
  for (const name of CLUSTER_NAMES) {
    (meta.groups[name] || []).forEach((id, index) => {
      state.assignment.set(id, { region: name, index });
    });
  }

  const margin = 40;
  const regions = {};
  regions.visual = { x: margin, y: margin, cols: meta.cols, rows: meta.rows, label: `视觉输入 0–${meta.visual_size - 1}` };

  let y = margin + meta.rows * GAP + 60;
  let x = margin;
  for (const name of CLUSTER_NAMES) {
    const count = (meta.groups[name] || []).length || 1;
    const rows = Math.ceil(count / CLUSTER_COLS);
    regions[name] = { x, y, cols: CLUSTER_COLS, rows, label: `${CLUSTER_LABELS[name]}（${count}）` };
    x += CLUSTER_COLS * GAP + 60;
  }

  y += Math.ceil((meta.visual_size || 1) / CLUSTER_COLS) * GAP + 60;
  regions.others = { x: margin, y, cols: OTHERS_COLS, rows: 1, label: '其余网络节点' };
  state.regions = regions;

  document.getElementById('grid-size').textContent = `${meta.rows}×${meta.cols}`;
  layoutPongCanvas();
}

function positionOf(id) {
  const cached = state.positions.get(id);
  if (cached) return cached;
  let entry = state.assignment.get(id);
  if (!entry) {
    entry = { region: 'others', index: state.others.length };
    state.others.push(id);
    state.assignment.set(id, entry);
  }
  const region = state.regions[entry.region];
  const col = entry.index % region.cols;
  const row = Math.floor(entry.index / region.cols);
  const position = {
    x: region.x + col * GAP + GAP / 2,
    y: region.y + row * GAP + GAP / 2,
  };
  state.positions.set(id, position);
  return position;
}

function isSpecial(id) {
  const entry = state.assignment.get(id);
  return Boolean(entry) && entry.region !== 'others';
}

/* ---------- 画布尺寸 ---------- */

function layoutPongCanvas() {
  if (!state.meta) return;
  const cell = Math.max(6, Math.floor(pongCanvas.clientWidth / state.meta.cols));
  pongCanvas.width = cell * state.meta.cols;
  pongCanvas.height = cell * state.meta.rows;
  pongCtx.imageSmoothingEnabled = false;
  drawPong(null);
}

function resizeGraph() {
  dpr = window.devicePixelRatio || 1;
  width = graphCanvas.clientWidth;
  height = graphCanvas.clientHeight;
  graphCanvas.width = Math.floor(width * dpr);
  graphCanvas.height = Math.floor(height * dpr);
  if (state.fitPending && state.positions.size) fitView();
  render();
}

/* ---------- 视图 ---------- */

function fitView() {
  if (!state.positions.size) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const position of state.positions.values()) {
    minX = Math.min(minX, position.x);
    minY = Math.min(minY, position.y);
    maxX = Math.max(maxX, position.x);
    maxY = Math.max(maxY, position.y);
  }
  const pad = 40;
  const scale = Math.min((width - pad) / (maxX - minX + pad), (height - pad) / (maxY - minY + pad));
  state.view.scale = Math.max(0.05, Math.min(2.5, scale));
  state.view.x = pad / 2 - minX * state.view.scale;
  state.view.y = pad / 2 - minY * state.view.scale;
  state.fitPending = false;
  render();
}

function toWorld(clientX, clientY) {
  const rect = graphCanvas.getBoundingClientRect();
  return {
    x: (clientX - rect.left - state.view.x) / state.view.scale,
    y: (clientY - rect.top - state.view.y) / state.view.scale,
  };
}

function centerOn(id) {
  const position = positionOf(id);
  state.view.x = width / 2 - position.x * state.view.scale;
  state.view.y = height / 2 - position.y * state.view.scale;
  render();
}

/* ---------- 渲染 ---------- */

function circlePath(ctx, points) {
  ctx.beginPath();
  for (const point of points) {
    ctx.moveTo(point.x + RADIUS, point.y);
    ctx.arc(point.x, point.y, RADIUS, 0, Math.PI * 2);
  }
}

function drawRegions(ctx) {
  ctx.lineWidth = 1 / state.view.scale;
  for (const region of Object.values(state.regions)) {
    const w = region.cols * GAP;
    const rows = region.dynamic ? Math.max(1, Math.ceil(state.others.length / region.cols)) : region.rows;
    const h = rows * GAP;
    ctx.strokeStyle = 'rgba(51,65,85,0.9)';
    ctx.strokeRect(region.x - GAP / 2, region.y - GAP / 2, w, h);
    ctx.fillStyle = 'rgba(148,163,184,0.85)';
    ctx.font = `${12 / state.view.scale}px sans-serif`;
    ctx.fillText(region.label, region.x - GAP / 2, region.y - GAP / 2 - 6 / state.view.scale);
  }
}

function drawValueLabels(ctx) {
  const scale = state.view.scale;
  const manual = document.getElementById('values').checked;
  if (!manual && scale < 1.4) return;
  ctx.fillStyle = '#e2e8f0';
  ctx.font = `${9 / scale}px monospace`;
  const left = -state.view.x / scale;
  const top = -state.view.y / scale;
  const right = left + width / scale;
  const bottom = top + height / scale;
  let drawn = 0;
  for (const [id, node] of state.nodeData) {
    if (drawn >= 400) break;
    const position = state.positions.get(id);
    if (!position) continue;
    if (position.x < left || position.x > right || position.y < top || position.y > bottom) continue;
    ctx.fillText(node.value.toFixed(3), position.x + RADIUS + 2, position.y - RADIUS);
    drawn += 1;
  }
}

function render() {
  renderQueued = false;
  graphCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  graphCtx.clearRect(0, 0, width, height);
  if (!state.meta) return;
  graphCtx.save();
  graphCtx.translate(state.view.x, state.view.y);
  graphCtx.scale(state.view.scale, state.view.scale);

  drawRegions(graphCtx);

  const active = [];
  const inactive = [];
  const special = [];
  for (const [id, node] of state.nodeData) {
    const position = state.positions.get(id);
    if (!position) continue;
    (node.active ? active : inactive).push(position);
    if (isSpecial(id)) special.push(position);
  }
  graphCtx.fillStyle = '#dc2626';
  circlePath(graphCtx, inactive);
  graphCtx.fill();
  graphCtx.fillStyle = '#22c55e';
  circlePath(graphCtx, active);
  graphCtx.fill();
  if (special.length) {
    graphCtx.strokeStyle = 'rgba(250,204,21,0.5)';
    graphCtx.lineWidth = 1 / state.view.scale;
    circlePath(graphCtx, special);
    graphCtx.stroke();
  }

  drawValueLabels(graphCtx);

  const marker = state.pinned || state.hover;
  if (marker !== null && state.positions.has(marker)) {
    const position = state.positions.get(marker);
    graphCtx.strokeStyle = '#38bdf8';
    graphCtx.lineWidth = 2 / state.view.scale;
    graphCtx.beginPath();
    graphCtx.arc(position.x, position.y, RADIUS + 4, 0, Math.PI * 2);
    graphCtx.stroke();
  }

  graphCtx.restore();
}

function queueRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(render);
}

/* ---------- Pong ---------- */

function drawPong(game) {
  if (!state.meta) return;
  const cell = pongCanvas.height / state.meta.rows;
  pongCtx.fillStyle = '#000';
  pongCtx.fillRect(0, 0, pongCanvas.width, pongCanvas.height);
  if (!game) return;
  pongCtx.fillStyle = '#f8fafc';
  for (let row = 0; row < state.meta.rows; row += 1) {
    for (let col = 0; col < state.meta.cols; col += 1) {
      if (game.grid[row][col]) pongCtx.fillRect(col * cell, row * cell, cell, cell);
    }
  }
}

/* ---------- 详情 ---------- */

function describe(id) {
  const node = state.nodeData.get(id);
  if (!node) return `ID ${id}：尚未创建`;
  const entry = state.assignment.get(id);
  const role = entry
    ? (entry.region === 'visual' ? `视觉 (${Math.floor(id / state.meta.cols)}, ${id % state.meta.cols})` : CLUSTER_LABELS[entry.region] + ` #${entry.index + 1}`)
    : '普通网络节点';
  const out = state.outEdges.get(id) || [];
  const lines = out
    .slice()
    .sort((a, b) => b.w - a.w)
    .slice(0, 40)
    .map((edge) => `  → ${edge.dst}  权重 ${edge.w.toFixed(3)}`);
  return `ID ${id} | ${role} | ${node.active ? '激活' : '未激活'}\n`
    + `value = ${node.value.toFixed(6)}\n`
    + `前向连接 ${out.length} 条${out.length > 40 ? '（显示前 40）' : ''}\n`
    + (lines.join('\n') || '  （暂无）');
}

function updateDetail() {
  const id = state.pinned !== null ? state.pinned : state.hover;
  const box = document.getElementById('detail');
  box.textContent = id === null ? '悬停或点击节点查看详情' : describe(id);
}

/* ---------- 数据接入 ---------- */

let lastUpTime = 0;
let lastDownTime = 0;

function handleState(message) {
  const { game, net } = message;
  const now = performance.now();
  const running = !net.stopped;
  if (running && game.up > 0) lastUpTime = now;
  if (running && game.down > 0) lastDownTime = now;

  const upBox = document.getElementById('ind-up');
  const downBox = document.getElementById('ind-down');
  upBox.classList.toggle('on', running && now - lastUpTime < 500);
  downBox.classList.toggle('on', running && now - lastDownTime < 500);
  upBox.querySelector('b').textContent = game.up;
  downBox.querySelector('b').textContent = game.down;

  document.getElementById('score').textContent = `${game.score_left} : ${game.score_right}`;
  drawPong(game);

  document.getElementById('stats').textContent =
    `${net.stopped ? (net.saved ? '已停止并落盘' : '正在停止') : '运行中'}`
    + ` | 节点 ${net.nodes} | 激活 ${net.active} | 连接 ${net.edges}`
    + ` | dopamine ${net.dopamine.toFixed(3)}`
    + (net.save_error ? ` | 保存异常：${net.save_error}` : '');
}

/** 更新图统计文案，全量与增量帧共用。 */
function updateGraphStats() {
  document.getElementById('graph-stats').textContent =
    `图：${state.nodeData.size} 节点 / ${state.edgeCount} 边`;
}

/** 收到全量图帧：重建节点表与出边表，并重新统计连接数。 */
function handleGraph(message) {
  state.nodeData = new Map(message.nodes.map(([id, active, value]) => [id, { active: active === 1, value }]));
  const outEdges = new Map();
  let count = 0;
  for (const [src, dst, w] of message.edges) {
    let list = outEdges.get(src);
    if (!list) {
      list = [];
      outEdges.set(src, list);
    }
    list.push({ dst, w });
    count += 1;
  }
  state.outEdges = outEdges;
  state.edgeCount = count;

  updateGraphStats();
  for (const id of state.nodeData.keys()) positionOf(id);
  if (state.fitPending) fitView();
  queueRender();
  updateDetail();
}

/**
 * 收到增量图帧：只更新发生变化的节点。
 * patch.nodes 里出现的每个节点，其出边都以 patch.edges 中的记录为准（可能为空）。
 */
function handlePatch(message) {
  let added = 0;
  for (const [id, active, value] of message.nodes) {
    const node = state.nodeData.get(id);
    if (node) {
      node.active = active === 1;
      node.value = value;
    } else {
      state.nodeData.set(id, { active: active === 1, value });
      added += 1;
    }
  }

  // 脏节点的旧出边先整体移除，再按增量帧重建，保证删除/新增都被正确反映。
  const touched = new Set(message.nodes.map(([id]) => id));
  let count = 0;
  for (const src of touched) {
    const old = state.outEdges.get(src);
    if (old) count -= old.length;
    state.outEdges.delete(src);
  }
  for (const [src, dst, w] of message.edges) {
    let list = state.outEdges.get(src);
    if (!list) {
      list = [];
      state.outEdges.set(src, list);
    }
    list.push({ dst, w });
    count += 1;
  }
  state.edgeCount += count;

  updateGraphStats();
  if (added) {
    for (const id of touched) positionOf(id);
    if (state.fitPending) fitView();
  }
  queueRender();
  updateDetail();
}

function handle(message) {
  if (message.type === 'started') {
    startButton.disabled = true;
    stopButton.disabled = false;
    stopButton.classList.remove('done', 'failed');
    stopButton.textContent = '强制结束网络并落盘';
    document.getElementById('stats').textContent = '网络启动中…';
  } else if (message.type === 'meta') {
    buildLayout(message);
    state.fitPending = true;
  } else if (message.type === 'state') {
    handleState(message);
  } else if (message.type === 'graph') {
    handleGraph(message);
  } else if (message.type === 'patch') {
    handlePatch(message);
  } else if (message.type === 'stopped') {
    clearInterval(stopTimer);
    stopTimer = null;
    const button = document.getElementById('stop');
    button.classList.remove('failed');
    button.classList.add('done');
    button.textContent = '网络已结束并落盘';
    button.disabled = true;
    document.getElementById('ind-up').classList.remove('on');
    document.getElementById('ind-down').classList.remove('on');
  } else if (message.type === 'bridge_error') {
    document.getElementById('stats').textContent = `网络启动失败：${message.message}`;
  } else if (message.type === 'exit') {
    clearInterval(stopTimer);
    stopTimer = null;
    startButton.disabled = false;
    stopButton.disabled = true;
    document.getElementById('stats').textContent += ' | Python 进程已退出';
  }
}

/* ---------- 交互 ---------- */

const startButton = document.getElementById('start');
const stopButton = document.getElementById('stop');
let stopTimer = null;
stopButton.disabled = true;

startButton.addEventListener('click', async () => {
  startButton.disabled = true;
  try {
    const response = await fetch('/start', { method: 'POST' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (error) {
    startButton.disabled = false;
    document.getElementById('stats').textContent = `启动失败：${error.message}`;
  }
});

stopButton.addEventListener('click', async () => {
  stopButton.disabled = true;
  const started = Date.now();
  const tick = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    stopButton.textContent = `正在等待激活线程结束并落盘… ${seconds}s`;
  };
  tick();
  stopTimer = setInterval(tick, 1000);
  try {
    const response = await fetch('/stop', { method: 'POST' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (error) {
    clearInterval(stopTimer);
    stopTimer = null;
    stopButton.disabled = false;
    stopButton.classList.add('failed');
    stopButton.textContent = `取消失败，点击重试：${error.message}`;
  }
});

document.getElementById('fit').addEventListener('click', fitView);
document.getElementById('values').addEventListener('change', queueRender);

document.getElementById('search').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  const id = Number(event.target.value.trim());
  if (!Number.isFinite(id) || !state.meta) return;
  state.pinned = id;
  state.view.scale = Math.max(state.view.scale, 1.2);
  centerOn(id);
  updateDetail();
});

let dragging = false;
let dragStart = null;
graphCanvas.addEventListener('mousedown', (event) => {
  dragging = true;
  dragStart = { x: event.clientX - state.view.x, y: event.clientY - state.view.y };
  graphCanvas.classList.add('dragging');
});
window.addEventListener('mouseup', () => {
  dragging = false;
  graphCanvas.classList.remove('dragging');
});
window.addEventListener('mousemove', (event) => {
  if (dragging && dragStart) {
    state.view.x = event.clientX - dragStart.x;
    state.view.y = event.clientY - dragStart.y;
    queueRender();
  }
});

graphCanvas.addEventListener('wheel', (event) => {
  event.preventDefault();
  const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
  const before = toWorld(event.clientX, event.clientY);
  state.view.scale = Math.max(0.05, Math.min(6, state.view.scale * factor));
  const after = toWorld(event.clientX, event.clientY);
  state.view.x += (after.x - before.x) * state.view.scale;
  state.view.y += (after.y - before.y) * state.view.scale;
  queueRender();
}, { passive: false });

function pickNode(clientX, clientY) {
  const point = toWorld(clientX, clientY);
  const radius = Math.max(6 / state.view.scale, RADIUS + 2);
  let best = null;
  let bestDistance = radius;
  for (const id of state.nodeData.keys()) {
    const position = state.positions.get(id);
    if (!position) continue;
    const distance = Math.hypot(position.x - point.x, position.y - point.y);
    if (distance <= bestDistance) {
      bestDistance = distance;
      best = id;
    }
  }
  return best;
}

let hoverQueued = false;
let hoverPoint = null;
graphCanvas.addEventListener('mousemove', (event) => {
  hoverPoint = { x: event.clientX, y: event.clientY };
  if (hoverQueued) return;
  hoverQueued = true;
  requestAnimationFrame(() => {
    hoverQueued = false;
    if (!state.meta) return;
    const id = pickNode(hoverPoint.x, hoverPoint.y);
    if (id !== state.hover) {
      state.hover = id;
      updateDetail();
      queueRender();
    }
  });
});

graphCanvas.addEventListener('click', (event) => {
  const id = pickNode(event.clientX, event.clientY);
  state.pinned = id === state.pinned ? null : id;
  updateDetail();
  queueRender();
});

new ResizeObserver(resizeGraph).observe(graphCanvas);
const source = new EventSource('/events');
source.onmessage = (event) => handle(JSON.parse(event.data));
resizeGraph();
