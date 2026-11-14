'use strict';

// 三维网络图：访问页面时从 /api/graph 读取一次数据库，客户端完成布局与渲染。

const canvas = document.getElementById('scene');
const context = canvas.getContext('2d');
const loadingEl = document.getElementById('loading');
const loadingText = document.getElementById('loading-text');
const statsEl = document.getElementById('stats');
const detailEl = document.getElementById('detail');
const tooltipEl = document.getElementById('tooltip');

const SPACING = 3.2;
const REPULSION = 0.9;
const SPRING = 0.10;
const GRAVITY = 0.0015;
const DAMPING = 0.85;

// 半邻域偏移：每个格子只扫描字典序正方向的邻居，避免同一对被计算两次。
const HALF_OFFSETS = [
  [0, 0, 0], [1, 0, 0],
  [-1, 1, 0], [0, 1, 0], [1, 1, 0],
  [-1, -1, 1], [0, -1, 1], [1, -1, 1],
  [-1, 0, 1], [0, 0, 1], [1, 0, 1],
  [-1, 1, 1], [0, 1, 1], [1, 1, 1],
];

let ids = new Int32Array(0);
let values = new Float32Array(0);
let degree = new Int32Array(0);
let px = new Float32Array(0);
let py = new Float32Array(0);
let pz = new Float32Array(0);
let vx = new Float32Array(0);
let vy = new Float32Array(0);
let vz = new Float32Array(0);
let fx = new Float32Array(0);
let fy = new Float32Array(0);
let fz = new Float32Array(0);
let edgeS = new Int32Array(0);
let edgeT = new Int32Array(0);
let edgeW = new Float32Array(0);
let edgeCount = 0;
let adjacency = [];
let nodeCount = 0;
let maxDegree = 1;

let sx = new Float32Array(0);
let sy = new Float32Array(0);
let depthZ = new Float32Array(0);
let persp = new Float32Array(0);

let gridCounts = new Int32Array(0);
let gridStart = new Int32Array(0);
let gridCursor = new Int32Array(0);
let gridItems = new Int32Array(0);
let gridNx = 1;
let gridNy = 1;
let gridNz = 1;
let gridCell = SPACING;
let gridMinX = 0;
let gridMinY = 0;
let gridMinZ = 0;

let ready = false;
let renderQueued = false;
let yaw = 0.6;
let pitch = -0.35;
let zoom = 1;
let hovered = -1;
let selected = -1;
let dragging = false;
let moved = 0;
let lastX = 0;
let lastY = 0;
let layoutMs = 0;

function clamp(value, low, high) {
  return value < low ? low : value > high ? high : value;
}

function mulberry32(seed) {
  let state = seed >>> 0;
  return function random() {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function showMessage(message) {
  loadingEl.classList.remove('hidden');
  loadingEl.querySelector('.spinner').style.display = 'none';
  loadingText.textContent = message;
}

function prepare(payload) {
  const nodeRows = payload.nodes || [];
  const edgeRows = payload.edges || [];
  nodeCount = nodeRows.length;

  ids = new Int32Array(nodeCount);
  values = new Float32Array(nodeCount);
  const index = new Map();
  for (let i = 0; i < nodeCount; i++) {
    ids[i] = nodeRows[i][0];
    const value = Number(nodeRows[i][1]);
    values[i] = Number.isFinite(value) ? clamp(value, 0, 1) : 0;
    index.set(ids[i], i);
  }

  edgeS = new Int32Array(edgeRows.length);
  edgeT = new Int32Array(edgeRows.length);
  edgeW = new Float32Array(edgeRows.length);
  adjacency = Array.from({ length: nodeCount }, () => []);
  degree = new Int32Array(nodeCount);
  let count = 0;
  for (const row of edgeRows) {
    const source = index.get(row[0]);
    const target = index.get(row[1]);
    if (source === undefined || target === undefined || source === target) continue;
    edgeS[count] = source;
    edgeT[count] = target;
    edgeW[count] = Number(row[2]) || 0;
    adjacency[source].push(count);
    adjacency[target].push(count);
    degree[source] += 1;
    degree[target] += 1;
    count += 1;
  }
  edgeCount = count;
  edgeS = edgeS.subarray(0, count);
  edgeT = edgeT.subarray(0, count);
  edgeW = edgeW.subarray(0, count);
  maxDegree = 1;
  for (let i = 0; i < nodeCount; i++) {
    if (degree[i] > maxDegree) maxDegree = degree[i];
  }

  px = new Float32Array(nodeCount);
  py = new Float32Array(nodeCount);
  pz = new Float32Array(nodeCount);
  vx = new Float32Array(nodeCount);
  vy = new Float32Array(nodeCount);
  vz = new Float32Array(nodeCount);
  fx = new Float32Array(nodeCount);
  fy = new Float32Array(nodeCount);
  fz = new Float32Array(nodeCount);
  sx = new Float32Array(nodeCount);
  sy = new Float32Array(nodeCount);
  depthZ = new Float32Array(nodeCount);
  persp = new Float32Array(nodeCount);

  const random = mulberry32(0x9e3779b9);
  const radius = SPACING * Math.cbrt(Math.max(nodeCount, 1)) * 0.85;
  for (let i = 0; i < nodeCount; i++) {
    const theta = random() * Math.PI * 2;
    const phi = Math.acos(2 * random() - 1);
    const distance = Math.cbrt(random()) * radius;
    px[i] = Math.sin(phi) * Math.cos(theta) * distance;
    py[i] = Math.sin(phi) * Math.sin(theta) * distance;
    pz[i] = Math.cos(phi) * distance;
  }
}

function rebuildGrid() {
  let minX = Infinity;
  let minY = Infinity;
  let minZ = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  let maxZ = -Infinity;
  for (let i = 0; i < nodeCount; i++) {
    if (px[i] < minX) minX = px[i];
    if (py[i] < minY) minY = py[i];
    if (pz[i] < minZ) minZ = pz[i];
    if (px[i] > maxX) maxX = px[i];
    if (py[i] > maxY) maxY = py[i];
    if (pz[i] > maxZ) maxZ = pz[i];
  }
  if (!Number.isFinite(minX)) {
    minX = minY = minZ = 0;
    maxX = maxY = maxZ = 1;
  }
  const spanX = Math.max(maxX - minX, 1e-4);
  const spanY = Math.max(maxY - minY, 1e-4);
  const spanZ = Math.max(maxZ - minZ, 1e-4);
  const limit = 72;
  gridCell = Math.max(SPACING, spanX / limit, spanY / limit, spanZ / limit);
  gridNx = Math.min(limit, Math.floor(spanX / gridCell) + 1);
  gridNy = Math.min(limit, Math.floor(spanY / gridCell) + 1);
  gridNz = Math.min(limit, Math.floor(spanZ / gridCell) + 1);
  gridMinX = minX;
  gridMinY = minY;
  gridMinZ = minZ;

  const cells = gridNx * gridNy * gridNz;
  if (gridCounts.length < cells) {
    gridCounts = new Int32Array(cells);
    gridStart = new Int32Array(cells + 1);
    gridCursor = new Int32Array(cells);
    gridItems = new Int32Array(Math.max(nodeCount, 1));
  } else {
    gridCounts.fill(0, 0, cells);
  }

  for (let i = 0; i < nodeCount; i++) {
    const ix = clamp(Math.floor((px[i] - gridMinX) / gridCell), 0, gridNx - 1);
    const iy = clamp(Math.floor((py[i] - gridMinY) / gridCell), 0, gridNy - 1);
    const iz = clamp(Math.floor((pz[i] - gridMinZ) / gridCell), 0, gridNz - 1);
    gridCounts[(iz * gridNy + iy) * gridNx + ix] += 1;
  }

  let total = 0;
  for (let cell = 0; cell < cells; cell++) {
    gridStart[cell] = total;
    total += gridCounts[cell];
  }
  gridStart[cells] = total;
  gridCursor.set(gridStart.subarray(0, cells));

  for (let i = 0; i < nodeCount; i++) {
    const ix = clamp(Math.floor((px[i] - gridMinX) / gridCell), 0, gridNx - 1);
    const iy = clamp(Math.floor((py[i] - gridMinY) / gridCell), 0, gridNy - 1);
    const iz = clamp(Math.floor((pz[i] - gridMinZ) / gridCell), 0, gridNz - 1);
    gridItems[gridCursor[(iz * gridNy + iy) * gridNx + ix]++] = i;
  }
}

function simulate() {
  fx.fill(0);
  fy.fill(0);
  fz.fill(0);
  rebuildGrid();

  const limitSquared = SPACING * SPACING;
  for (let i = 0; i < nodeCount; i++) {
    const ix = clamp(Math.floor((px[i] - gridMinX) / gridCell), 0, gridNx - 1);
    const iy = clamp(Math.floor((py[i] - gridMinY) / gridCell), 0, gridNy - 1);
    const iz = clamp(Math.floor((pz[i] - gridMinZ) / gridCell), 0, gridNz - 1);
    for (let offset = 0; offset < HALF_OFFSETS.length; offset++) {
      const ox = HALF_OFFSETS[offset][0];
      const oy = HALF_OFFSETS[offset][1];
      const oz = HALF_OFFSETS[offset][2];
      const cx = ix + ox;
      const cy = iy + oy;
      const cz = iz + oz;
      if (cx < 0 || cx >= gridNx || cy < 0 || cy >= gridNy || cz < 0 || cz >= gridNz) continue;
      const sameCell = ox === 0 && oy === 0 && oz === 0;
      const cell = (cz * gridNy + cy) * gridNx + cx;
      for (let k = gridStart[cell]; k < gridStart[cell + 1]; k++) {
        const j = gridItems[k];
        if (sameCell && j <= i) continue;
        const ddx = px[j] - px[i];
        const ddy = py[j] - py[i];
        const ddz = pz[j] - pz[i];
        const distanceSquared = ddx * ddx + ddy * ddy + ddz * ddz;
        if (distanceSquared >= limitSquared || distanceSquared === 0) continue;
        const distance = Math.sqrt(distanceSquared);
        const push = (1 - distance / SPACING) * REPULSION / distance;
        fx[i] -= ddx * push;
        fy[i] -= ddy * push;
        fz[i] -= ddz * push;
        fx[j] += ddx * push;
        fy[j] += ddy * push;
        fz[j] += ddz * push;
      }
    }
  }

  for (let edge = 0; edge < edgeCount; edge++) {
    const a = edgeS[edge];
    const b = edgeT[edge];
    const ddx = px[b] - px[a];
    const ddy = py[b] - py[a];
    const ddz = pz[b] - pz[a];
    const distance = Math.sqrt(ddx * ddx + ddy * ddy + ddz * ddz) || 1e-3;
    const force = (distance - SPACING) * SPRING / distance;
    fx[a] += ddx * force;
    fy[a] += ddy * force;
    fz[a] += ddz * force;
    fx[b] -= ddx * force;
    fy[b] -= ddy * force;
    fz[b] -= ddz * force;
  }

  for (let i = 0; i < nodeCount; i++) {
    fx[i] -= px[i] * GRAVITY;
    fy[i] -= py[i] * GRAVITY;
    fz[i] -= pz[i] * GRAVITY;
    vx[i] = (vx[i] + fx[i]) * DAMPING;
    vy[i] = (vy[i] + fy[i]) * DAMPING;
    vz[i] = (vz[i] + fz[i]) * DAMPING;
    px[i] += clamp(vx[i], -SPACING, SPACING);
    py[i] += clamp(vy[i], -SPACING, SPACING);
    pz[i] += clamp(vz[i], -SPACING, SPACING);
  }
}

function finishLayout(elapsed) {
  layoutMs = elapsed;
  let centerX = 0;
  let centerY = 0;
  let centerZ = 0;
  for (let i = 0; i < nodeCount; i++) {
    centerX += px[i];
    centerY += py[i];
    centerZ += pz[i];
  }
  centerX /= Math.max(nodeCount, 1);
  centerY /= Math.max(nodeCount, 1);
  centerZ /= Math.max(nodeCount, 1);

  const distances = new Float64Array(nodeCount);
  for (let i = 0; i < nodeCount; i++) {
    const dx = px[i] - centerX;
    const dy = py[i] - centerY;
    const dz = pz[i] - centerZ;
    distances[i] = Math.sqrt(dx * dx + dy * dy + dz * dz);
  }
  distances.sort();
  const radius = distances[Math.floor(nodeCount * 0.95)] || 1;

  for (let i = 0; i < nodeCount; i++) {
    px[i] = (px[i] - centerX) / radius;
    py[i] = (py[i] - centerY) / radius;
    pz[i] = (pz[i] - centerZ) / radius;
  }

  ready = true;
  zoom = defaultZoom();
  loadingEl.classList.add('hidden');
  statsEl.textContent = `${nodeCount.toLocaleString()} 节点 · ${edgeCount.toLocaleString()} 连接 · 布局 ${Math.round(layoutMs)} ms · 滚轮缩放查看细节`;
  requestRender();
}

function defaultZoom() {
  return nodeCount > 20000 ? 2.2 : nodeCount > 5000 ? 1.5 : 1;
}

function startLayout() {
  const iterations = nodeCount > 30000 ? 60 : nodeCount > 10000 ? 96 : 150;
  const chunk = nodeCount > 30000 ? 6 : 8;
  let iteration = 0;
  const started = performance.now();
  loadingText.textContent = '计算三维布局… 0%';

  function step() {
    for (let k = 0; k < chunk && iteration < iterations; k++, iteration++) {
      simulate();
    }
    if (iteration < iterations) {
      loadingText.textContent = `计算三维布局… ${Math.round(iteration / iterations * 100)}%`;
      requestAnimationFrame(step);
    } else {
      finishLayout(performance.now() - started);
    }
  }
  requestAnimationFrame(step);
}

function requestRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(render);
}

function render() {
  renderQueued = false;
  if (!ready) return;

  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const centerX = width / 2;
  const centerY = height / 2;
  const view = Math.min(width, height) * 0.52;
  const cosY = Math.cos(yaw);
  const sinY = Math.sin(yaw);
  const cosP = Math.cos(pitch);
  const sinP = Math.sin(pitch);
  const focal = 2.4;
  const camera = 2.4;

  for (let i = 0; i < nodeCount; i++) {
    const x1 = px[i] * cosY - pz[i] * sinY;
    const z1 = px[i] * sinY + pz[i] * cosY;
    const y1 = py[i] * cosP - z1 * sinP;
    const z2 = py[i] * sinP + z1 * cosP;
    const scale = focal / (focal + camera + z2);
    persp[i] = scale;
    depthZ[i] = z2;
    sx[i] = centerX + x1 * scale * view * zoom;
    sy[i] = centerY + y1 * scale * view * zoom;
  }

  drawNodes();
  drawEdges();
  drawHighlight();
}

function drawEdges() {
  if (edgeCount === 0) return;
  const buckets = [new Path2D(), new Path2D(), new Path2D(), new Path2D(), new Path2D()];
  for (let edge = 0; edge < edgeCount; edge++) {
    const a = edgeS[edge];
    const b = edgeT[edge];
    const depth = (depthZ[a] + depthZ[b]) * 0.5;
    const bucket = clamp(Math.floor((depth + 1) * 2.5), 0, 4);
    const path = buckets[bucket];
    path.moveTo(sx[a], sy[a]);
    path.lineTo(sx[b], sy[b]);
  }
  const alphas = [0.26, 0.16, 0.1, 0.055, 0.028];
  context.lineWidth = 0.6;
  for (let bucket = 0; bucket < buckets.length; bucket++) {
    context.strokeStyle = `rgba(122, 170, 220, ${alphas[bucket]})`;
    context.stroke(buckets[bucket]);
  }
}

function drawNodes() {
  if (nodeCount === 0) return;
  const base = nodeCount > 20000 ? 1.2 : nodeCount > 5000 ? 1.8 : 2.6;
  const colorBuckets = 6;
  const depthBuckets = 3;
  const paths = Array.from({ length: colorBuckets * depthBuckets }, () => new Path2D());
  const degreeScale = 1 / Math.log1p(maxDegree);

  for (let i = 0; i < nodeCount; i++) {
    const weight = Math.log1p(degree[i]) * degreeScale;
    const colorIndex = clamp(Math.floor(weight * colorBuckets), 0, colorBuckets - 1);
    const depthIndex = clamp(Math.floor((depthZ[i] + 1) / 2 * depthBuckets), 0, depthBuckets - 1);
    const grow = 0.7 + Math.min(degree[i], 40) * 0.05;
    const size = clamp(base * grow * (persp[i] / 0.5), 0.6, 9);
    const path = paths[colorIndex * depthBuckets + depthIndex];
    path.rect(sx[i] - size / 2, sy[i] - size / 2, size, size);
  }

  const alphas = [0.96, 0.74, 0.5];
  for (let colorIndex = 0; colorIndex < colorBuckets; colorIndex++) {
    const weight = (colorIndex + 0.5) / colorBuckets;
    const hue = 205 - 180 * weight;
    for (let depthIndex = 0; depthIndex < depthBuckets; depthIndex++) {
      context.fillStyle = `hsla(${hue}, 82%, 63%, ${alphas[depthIndex]})`;
      context.fill(paths[colorIndex * depthBuckets + depthIndex]);
    }
  }
}

function drawHighlight() {
  const index = hovered >= 0 ? hovered : selected;
  if (index < 0 || index >= nodeCount) return;

  const edges = adjacency[index];
  if (edges.length) {
    const path = new Path2D();
    for (const edge of edges) {
      const other = edgeS[edge] === index ? edgeT[edge] : edgeS[edge];
      path.moveTo(sx[index], sy[index]);
      path.lineTo(sx[other], sy[other]);
    }
    context.lineWidth = 1.1;
    context.strokeStyle = 'rgba(120, 210, 255, 0.85)';
    context.stroke(path);
  }

  const size = clamp(3.1 * (persp[index] / 0.5), 1.6, 10);
  context.beginPath();
  context.arc(sx[index], sy[index], size + 4, 0, Math.PI * 2);
  context.lineWidth = 1.4;
  context.strokeStyle = '#f0b429';
  context.stroke();

  context.fillStyle = '#e6edf3';
  context.font = '12px "Segoe UI", "Microsoft YaHei", sans-serif';
  context.textAlign = 'center';
  context.fillText(`#${ids[index]}`, sx[index], sy[index] - size - 8);
}

function pick(clientX, clientY) {
  let best = -1;
  let bestDistance = 18 * 18;
  let bestDepth = Infinity;
  for (let i = 0; i < nodeCount; i++) {
    const dx = sx[i] - clientX;
    const dy = sy[i] - clientY;
    const distanceSquared = dx * dx + dy * dy;
    if (distanceSquared < bestDistance || (distanceSquared === bestDistance && depthZ[i] < bestDepth)) {
      best = i;
      bestDistance = distanceSquared;
      bestDepth = depthZ[i];
    }
  }
  return best;
}

function showTooltip(index, clientX, clientY) {
  tooltipEl.style.display = 'block';
  tooltipEl.style.left = `${clientX + 14}px`;
  tooltipEl.style.top = `${clientY + 14}px`;
  tooltipEl.innerHTML = `#${ids[index]} · 数值 ${values[index].toFixed(4)} · 连接 ${adjacency[index].length}`;
}

function hideTooltip() {
  tooltipEl.style.display = 'none';
}

function showDetail(index) {
  const rows = adjacency[index]
    .map((edge) => {
      const other = edgeS[edge] === index ? edgeT[edge] : edgeS[edge];
      return [ids[other], edgeW[edge]];
    })
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8);

  const table = rows.length
    ? `<table>${rows.map(([target, weight]) => `<tr><td>→ #${target}</td><td>${weight.toFixed(2)}</td></tr>`).join('')}</table>`
    : '<div style="margin-top:8px">没有前向连接</div>';

  detailEl.innerHTML = `<strong>节点 #${ids[index]}</strong><br>数值 <code>${values[index].toFixed(6)}</code> · 连接 ${adjacency[index].length}${table}`;
}

canvas.addEventListener('pointerdown', (event) => {
  dragging = true;
  moved = 0;
  lastX = event.clientX;
  lastY = event.clientY;
  canvas.classList.add('dragging');
  canvas.setPointerCapture(event.pointerId);
});

canvas.addEventListener('pointermove', (event) => {
  if (dragging) {
    const dx = event.clientX - lastX;
    const dy = event.clientY - lastY;
    lastX = event.clientX;
    lastY = event.clientY;
    moved += Math.abs(dx) + Math.abs(dy);
    yaw += dx * 0.006;
    pitch = clamp(pitch + dy * 0.006, -1.45, 1.45);
    hideTooltip();
    requestRender();
    return;
  }
  if (!ready) return;
  const index = pick(event.clientX, event.clientY);
  if (index !== hovered) {
    hovered = index;
    requestRender();
  }
  if (index >= 0) {
    showTooltip(index, event.clientX, event.clientY);
  } else {
    hideTooltip();
  }
});

canvas.addEventListener('pointerup', (event) => {
  dragging = false;
  canvas.classList.remove('dragging');
  if (!ready || moved >= 4) return;
  const index = pick(event.clientX, event.clientY);
  selected = index;
  if (index >= 0) showDetail(index);
  else detailEl.textContent = '点击节点查看数值与前向连接';
  requestRender();
});

canvas.addEventListener('pointerleave', () => {
  hovered = -1;
  hideTooltip();
  requestRender();
});

canvas.addEventListener('wheel', (event) => {
  event.preventDefault();
  zoom = clamp(zoom * Math.exp(-event.deltaY * 0.0012), 0.15, 8);
  requestRender();
}, { passive: false });

canvas.addEventListener('dblclick', () => {
  yaw = 0.6;
  pitch = -0.35;
  zoom = defaultZoom();
  requestRender();
});

window.addEventListener('resize', requestRender);

fetch('/api/graph', { cache: 'no-store' })
  .then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  })
  .then((payload) => {
    if (payload.error) {
      showMessage(`读取数据库失败：${payload.error}`);
      return;
    }
    if (payload.missing) {
      showMessage(`未找到数据库：${payload.database || 'nodes.db'}`);
      return;
    }
    if (!payload.nodes || payload.nodes.length === 0) {
      showMessage('数据库中没有节点，请先在控制台启动网络。');
      return;
    }
    statsEl.textContent = '正在准备…';
    prepare(payload);
    requestAnimationFrame(() => startLayout());
  })
  .catch((error) => {
    showMessage(`读取数据库失败：${error.message}`);
  });
