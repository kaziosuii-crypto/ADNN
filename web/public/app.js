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

// 控制台已不再显示拓扑图：图相关元素可能不存在，这里全部做空值保护。
const graphCanvas = document.getElementById('graph');
const graphCtx = graphCanvas ? graphCanvas.getContext('2d') : null;
const pongCanvas = document.getElementById('pong');
const pongCtx = pongCanvas.getContext('2d');
// 高频状态帧用到的 DOM 引用只查一次，避免每帧重复查询，减少布局抖动。
const ui = {
  indUp: document.getElementById('ind-up'),
  indDown: document.getElementById('ind-down'),
  score: document.getElementById('score'),
  stats: document.getElementById('stats'),
  graphStats: document.getElementById('graph-stats'),
  detail: document.getElementById('detail'),
  values: document.getElementById('values'),
};
const uiUpCount = ui.indUp.querySelector('b');
const uiDownCount = ui.indDown.querySelector('b');
let dpr = 1;
let width = 0;
let height = 0;
let renderQueued = false;
// 全量重绘节流（项目硬约束：至少间隔 70ms），避免 20fps 状态帧把 4000 个节点重复画爆。
const RENDER_MIN_INTERVAL = 70;
let lastRenderAt = 0;
let renderTimer = null;
// 调试开关：地址带 ?debug=1 时在控制台输出渲染、路径重建与拾取耗时。
const DEBUG = new URLSearchParams(location.search).get('debug') === '1';
// 图数据版本号：每次全量/增量帧递增；激活色路径按它缓存。
let graphVersion = 0;
// 全部节点的圆点路径（世界坐标）：节点只增不减，新增节点用 addPath 追加，避免每个 patch 重放 5 万段圆弧。
const allNodesPath = { path: new Path2D() };
// 底图位图缓存：把“区域框 + 全部红点”一次性画进离屏画布；数据帧只 drawImage，不再逐帧栅格化全图。
const baseLayer = { canvas: null, ctx: null, valid: false, x: 0, y: 0, scale: 0, width: 0, height: 0 };
// 激活/特殊节点路径按数据版本缓存（只有几千个点，重建成本可接受）。
const pathCache = { version: -1, active: null, special: null };
// 拾取用的空间索引：世界坐标按 GAP*2 分桶；节点位置一旦分配就不再变化，新增节点增量写入。
const pickIndex = { cell: GAP * 2, buckets: new Map() };
// 当前激活节点 ID 集合与特殊节点 ID 列表：增量维护，避免每次 patch 遍历全部 5 万节点。
const activeIds = new Set();
const specialIds = [];

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

  // 特殊节点（视觉 + 四个集群）在 meta 阶段就固定下来，后续 others 增长不影响这份列表。
  specialIds.length = 0;
  for (const [id, entry] of state.assignment) {
    if (entry.region !== 'others') specialIds.push(id);
  }
  activeIds.clear();

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
  if (!graphCanvas) return;
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

/** 把一批世界坐标点压成一条圆点路径；Path2D 可跨帧复用，平移/缩放时无需重建。 */
function makeCirclePath(points) {
  const path = new Path2D();
  for (const point of points) {
    path.moveTo(point.x + RADIUS, point.y);
    path.arc(point.x, point.y, RADIUS, 0, Math.PI * 2);
  }
  return path;
}

/** 全量重建全部节点的红点路径（仅全量图帧调用）；同时让底图缓存失效。 */
function resetAllNodesPath() {
  const started = DEBUG ? performance.now() : 0;
  const path = new Path2D();
  for (const [id] of state.nodeData) {
    const position = state.positions.get(id);
    if (!position) continue;
    path.moveTo(position.x + RADIUS, position.y);
    path.arc(position.x, position.y, RADIUS, 0, Math.PI * 2);
  }
  allNodesPath.path = path;
  baseLayer.valid = false;
  if (DEBUG) {
    console.log('[app] resetAllNodesPath', state.nodeData.size, '节点，耗时',
      (performance.now() - started).toFixed(2), 'ms');
  }
}

/**
 * 把新增节点追加进红点路径，并在底图有效时同步补画进底图。
 * 这样每次 patch 只处理新增的几百个节点，不再重建 5 万段圆弧。
 */
function appendAllNodesPath(ids) {
  const delta = new Path2D();
  let count = 0;
  for (const id of ids) {
    const position = state.positions.get(id);
    if (!position) continue;
    delta.moveTo(position.x + RADIUS, position.y);
    delta.arc(position.x, position.y, RADIUS, 0, Math.PI * 2);
    count += 1;
  }
  if (!count) return;
  allNodesPath.path.addPath(delta);
  if (baseLayer.valid) {
    // 底图已经画好时，新节点的红点必须补画进去，否则 drawImage 会漏掉它们。
    const ctx = baseLayer.ctx;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.translate(state.view.x, state.view.y);
    ctx.scale(state.view.scale, state.view.scale);
    ctx.fillStyle = '#dc2626';
    ctx.fill(delta);
    ctx.restore();
  }
}

/** 按当前图数据版本重建激活/特殊节点路径；数据未变时直接复用缓存。 */
function buildPaths() {
  if (pathCache.version === graphVersion) return pathCache;
  const started = DEBUG ? performance.now() : 0;
  const active = [];
  for (const id of activeIds) {
    const position = state.positions.get(id);
    if (position) active.push(position);
  }
  // 特殊节点（视觉/集群）的黄圈保持原行为：全部特殊节点都描边，不随激活状态变化。
  const special = [];
  for (const id of specialIds) {
    const position = state.positions.get(id);
    if (position) special.push(position);
  }
  pathCache.active = makeCirclePath(active);
  pathCache.special = special.length ? makeCirclePath(special) : null;
  pathCache.version = graphVersion;
  if (DEBUG) {
    console.log('[app] buildPaths', state.nodeData.size, '节点', active.length, '激活，耗时',
      (performance.now() - started).toFixed(2), 'ms');
  }
  return pathCache;
}

/**
 * 取底图缓存（区域框 + 全部红点）。
 * 只有视图变换或画布尺寸变化时才重建；数据帧命中缓存时只做一次 drawImage。
 */
function ensureBaseLayer() {
  const view = state.view;
  if (baseLayer.valid
      && baseLayer.x === view.x
      && baseLayer.y === view.y
      && baseLayer.scale === view.scale
      && baseLayer.width === graphCanvas.width
      && baseLayer.height === graphCanvas.height) {
    return baseLayer.canvas;
  }
  const started = DEBUG ? performance.now() : 0;
  if (!baseLayer.canvas) {
    baseLayer.canvas = document.createElement('canvas');
    baseLayer.ctx = baseLayer.canvas.getContext('2d');
  }
  const canvas = baseLayer.canvas;
  canvas.width = graphCanvas.width;
  canvas.height = graphCanvas.height;
  const ctx = baseLayer.ctx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.translate(view.x, view.y);
  ctx.scale(view.scale, view.scale);
  ctx.fillStyle = '#dc2626';
  ctx.fill(allNodesPath.path);
  baseLayer.valid = true;
  baseLayer.x = view.x;
  baseLayer.y = view.y;
  baseLayer.scale = view.scale;
  baseLayer.width = canvas.width;
  baseLayer.height = canvas.height;
  if (DEBUG) {
    console.log('[app] buildBaseLayer', state.nodeData.size, '节点，耗时',
      (performance.now() - started).toFixed(2), 'ms');
  }
  return canvas;
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
  const manual = ui.values.checked;
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
  if (renderTimer !== null) {
    clearTimeout(renderTimer);
    renderTimer = null;
  }
  lastRenderAt = performance.now();
  graphCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  graphCtx.clearRect(0, 0, width, height);
  if (!state.meta) return;
  // 底图（全部节点的红点）来自离屏位图缓存：数据帧只做一次 drawImage，不再重放 5 万段圆弧。
  graphCtx.drawImage(ensureBaseLayer(), 0, 0, width, height);

  graphCtx.save();
  graphCtx.translate(state.view.x, state.view.y);
  graphCtx.scale(state.view.scale, state.view.scale);

  drawRegions(graphCtx);

  // 激活点与特殊节点只有几千个，按数据版本重建后叠加在底图之上。
  const paths = buildPaths();
  graphCtx.fillStyle = '#22c55e';
  graphCtx.fill(paths.active);
  if (paths.special) {
    graphCtx.strokeStyle = 'rgba(250,204,21,0.5)';
    graphCtx.lineWidth = 1 / state.view.scale;
    graphCtx.stroke(paths.special);
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
  if (DEBUG) {
    console.log('[app] render', (performance.now() - lastRenderAt).toFixed(2), 'ms，节点',
      state.nodeData.size, '缩放', state.view.scale.toFixed(3));
  }
}

/** 请求一次重绘：按 70ms 最小间隔节流，交互与数据帧共用同一条重绘通道。 */
function queueRender() {
  if (renderQueued) return;
  renderQueued = true;
  const wait = RENDER_MIN_INTERVAL - (performance.now() - lastRenderAt);
  if (wait <= 0) {
    requestAnimationFrame(render);
    return;
  }
  renderTimer = setTimeout(() => {
    renderTimer = null;
    requestAnimationFrame(render);
  }, wait);
}

/* ---------- Pong ---------- */

// Pong 像素缓冲：把 40×100 的格子先写进一张原始尺寸的离屏画布，再整体缩放贴到页面画布。
// 之前每帧调用 4000 次 fillRect，25fps 下就是每秒 10 万次绘制调用，是主线程卡顿的固定开销。
const pongBuffer = { canvas: null, ctx: null, image: null, pixels: null, rows: 0, cols: 0 };
// 小端机器上 Uint32 的字节序是 ABGR：#f8fafc -> 0xfffcfaf8，纯黑 -> 0xff000000。
const PONG_PIXEL_ON = 0xfffcfaf8;
const PONG_PIXEL_OFF = 0xff000000;

/** 按当前网格尺寸准备像素缓冲（尺寸变化时才重建）。 */
function ensurePongBuffer() {
  const rows = state.meta.rows;
  const cols = state.meta.cols;
  if (pongBuffer.canvas && pongBuffer.rows === rows && pongBuffer.cols === cols) {
    return pongBuffer;
  }
  const canvas = document.createElement('canvas');
  canvas.width = cols;
  canvas.height = rows;
  pongBuffer.canvas = canvas;
  pongBuffer.ctx = canvas.getContext('2d');
  pongBuffer.image = pongBuffer.ctx.createImageData(cols, rows);
  // 复用同一个 32 位视图，避免每帧新建类型化数组。
  pongBuffer.pixels = new Uint32Array(pongBuffer.image.data.buffer);
  pongBuffer.rows = rows;
  pongBuffer.cols = cols;
  return pongBuffer;
}

/** 画 Pong 画面：像素级写缓冲 + 一次 drawImage，避免逐格 fillRect 的高频调用。 */
function drawPong(game) {
  if (!state.meta) return;
  const rows = state.meta.rows;
  const cols = state.meta.cols;
  const buffer = ensurePongBuffer();
  const pixels = buffer.pixels;
  if (!game) {
    pixels.fill(PONG_PIXEL_OFF);
  } else {
    for (let row = 0; row < rows; row += 1) {
      const line = game.grid[row];
      const base = row * cols;
      for (let col = 0; col < cols; col += 1) {
        pixels[base + col] = line[col] ? PONG_PIXEL_ON : PONG_PIXEL_OFF;
      }
    }
  }
  buffer.ctx.putImageData(buffer.image, 0, 0);
  pongCtx.imageSmoothingEnabled = false;
  pongCtx.drawImage(buffer.canvas, 0, 0, pongCanvas.width, pongCanvas.height);
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
  if (!ui.detail) return;
  const id = state.pinned !== null ? state.pinned : state.hover;
  ui.detail.textContent = id === null ? '悬停或点击节点查看详情' : describe(id);
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

  ui.indUp.classList.toggle('on', running && now - lastUpTime < 500);
  ui.indDown.classList.toggle('on', running && now - lastDownTime < 500);
  uiUpCount.textContent = game.up;
  uiDownCount.textContent = game.down;

  ui.score.textContent = `${game.score_left} : ${game.score_right}`;
  drawPong(game);

  ui.stats.textContent =
    `${net.stopped ? (net.saved ? '已停止并落盘' : '正在停止') : '运行中'}`
    + ` | 节点 ${net.nodes} | 内部节点 ${net.genes}`
    + ` | 帧率 ${Number.isFinite(message.fps) ? message.fps.toFixed(1) : '-'}`
    + (net.save_error ? ` | 保存异常：${net.save_error}` : '');
}

/** 更新图统计文案，全量与增量帧共用。 */
function updateGraphStats() {
  if (!ui.graphStats) return;
  ui.graphStats.textContent = `图：${state.nodeData.size} 节点 / ${state.edgeCount} 边`;
}

/**
 * 兼容两种边数据格式：
 *   - 旧多进程实现：[[src, dst, w], ...]；
 *   - 原生引擎：扁平数组 [src, dst, w, src, dst, w, ...]（更省体积与解析时间）。
 */
function forEachEdge(edges, fn) {
  if (!edges || !edges.length) return;
  if (Array.isArray(edges[0])) {
    for (const [src, dst, w] of edges) fn(src, dst, w);
    return;
  }
  for (let i = 0; i + 2 < edges.length; i += 3) {
    fn(edges[i], edges[i + 1], edges[i + 2]);
  }
}

/** 收到全量图帧：重建节点表与出边表，并重新统计连接数。 */
function handleGraph(message) {
  state.nodeData = new Map(message.nodes.map(([id, active, value]) => [id, { active: active === 1, value }]));
  activeIds.clear();
  for (const [id, active] of message.nodes) {
    if (active === 1) activeIds.add(id);
  }
  const outEdges = new Map();
  let count = 0;
  forEachEdge(message.edges, (src, dst, w) => {
    let list = outEdges.get(src);
    if (!list) {
      list = [];
      outEdges.set(src, list);
    }
    list.push({ dst, w });
    count += 1;
  });
  state.outEdges = outEdges;
  state.edgeCount = count;
  graphVersion += 1;

  updateGraphStats();
  for (const id of state.nodeData.keys()) positionOf(id);
  // 全量帧：红点路径与拾取索引整表重建，底图缓存随之下次渲染重画。
  resetAllNodesPath();
  resetPickIndex();
  if (state.fitPending) fitView();
  queueRender();
  updateDetail();
}

/**
 * 收到增量图帧：只更新发生变化的节点。
 * patch.nodes 里出现的每个节点，其出边都以 patch.edges 中的记录为准（可能为空）。
 */
function handlePatch(message) {
  const addedIds = [];
  for (const [id, active, value] of message.nodes) {
    const node = state.nodeData.get(id);
    if (node) {
      node.active = active === 1;
      node.value = value;
    } else {
      state.nodeData.set(id, { active: active === 1, value });
      addedIds.push(id);
    }
    // 激活集合增量维护：按本帧状态写入（重复 add/delete 是空操作，成本可忽略）。
    if (active === 1) activeIds.add(id);
    else activeIds.delete(id);
  }

  // 只有增量帧里带了边数据的节点才允许覆盖出边表；纯状态变化的节点必须保留旧边，
  // 否则每次激活翻转都会把该节点的出边从详情面板和统计里清空。
  const touched = new Set(message.edgeNodes || message.nodes.map(([id]) => id));
  let count = 0;
  for (const src of touched) {
    const old = state.outEdges.get(src);
    if (old) count -= old.length;
    state.outEdges.delete(src);
  }
  forEachEdge(message.edges, (src, dst, w) => {
    let list = state.outEdges.get(src);
    if (!list) {
      list = [];
      state.outEdges.set(src, list);
    }
    list.push({ dst, w });
    count += 1;
  });
  state.edgeCount += count;
  graphVersion += 1;

  updateGraphStats();
  if (addedIds.length) {
    // 只给新增节点分配位置，并把它们增量追加进红点路径、底图与拾取索引。
    for (const id of addedIds) positionOf(id);
    appendAllNodesPath(addedIds);
    addPickIndex(addedIds);
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

// 工具栏属于拓扑图面板，已随面板一起移除，这里做存在性保护。
const fitButton = document.getElementById('fit');
if (fitButton) fitButton.addEventListener('click', fitView);
const valuesToggle = document.getElementById('values');
if (valuesToggle) valuesToggle.addEventListener('change', queueRender);

const searchInput = document.getElementById('search');
if (searchInput) {
  searchInput.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    const id = Number(event.target.value.trim());
    if (!Number.isFinite(id) || !state.meta) return;
    state.pinned = id;
    state.view.scale = Math.max(state.view.scale, 1.2);
    centerOn(id);
    updateDetail();
  });
}

// 拓扑图面板已从控制台移除：所有图交互只在 canvas 存在时绑定。
if (graphCanvas) {
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

/** 把单个节点写入拾取分桶。 */
function indexPosition(id, position) {
  const key = `${Math.floor(position.x / pickIndex.cell)},${Math.floor(position.y / pickIndex.cell)}`;
  let list = pickIndex.buckets.get(key);
  if (!list) {
    list = [];
    pickIndex.buckets.set(key, list);
  }
  list.push(id);
}

/** 全量重建拾取索引（全量图帧调用）。 */
function resetPickIndex() {
  const started = DEBUG ? performance.now() : 0;
  pickIndex.buckets = new Map();
  for (const [id] of state.nodeData) {
    const position = state.positions.get(id);
    if (position) indexPosition(id, position);
  }
  if (DEBUG) {
    console.log('[app] resetPickIndex', state.nodeData.size, '节点，', pickIndex.buckets.size,
      '个桶，耗时', (performance.now() - started).toFixed(2), 'ms');
  }
}

/** 增量写入新分配位置的节点，避免每次 patch 重建整张索引。 */
function addPickIndex(ids) {
  for (const id of ids) {
    const position = state.positions.get(id);
    if (position) indexPosition(id, position);
  }
}

/** 在鼠标位置附近找最近的节点；用分桶索引把 O(n) 扫描降到常数级。 */
function pickNode(clientX, clientY) {
  const point = toWorld(clientX, clientY);
  const radius = Math.max(6 / state.view.scale, RADIUS + 2);
  const cell = pickIndex.cell;
  const reach = Math.ceil(radius / cell);
  const bx = Math.floor(point.x / cell);
  const by = Math.floor(point.y / cell);
  let best = null;
  let bestDistance = radius;
  for (let gx = bx - reach; gx <= bx + reach; gx += 1) {
    for (let gy = by - reach; gy <= by + reach; gy += 1) {
      const list = pickIndex.buckets.get(`${gx},${gy}`);
      if (!list) continue;
      for (const id of list) {
        const position = state.positions.get(id);
        if (!position) continue;
        const dx = position.x - point.x;
        const dy = position.y - point.y;
        const distance = Math.sqrt(dx * dx + dy * dy);
        if (distance <= bestDistance) {
          bestDistance = distance;
          best = id;
        }
      }
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
}
const source = new EventSource('/events');
source.onmessage = (event) => handle(JSON.parse(event.data));
resizeGraph();
