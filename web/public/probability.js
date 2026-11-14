'use strict';

// 连接概率：访问页面时从 /api/probability 读取一次数据库。
// 概率按当前网络的传播规则计算：min(1, 连接权重)。

const cardsEl = document.getElementById('cards');
const metaEl = document.getElementById('meta');
const emptyEl = document.getElementById('empty');
const histogramCanvas = document.getElementById('histogram');
const stripCanvas = document.getElementById('strip');
const topEl = document.getElementById('top');

let probabilities = [];
let topEdges = [];
let nodeCount = 0;
let edgeCount = 0;
let sourceCount = 0;

function clamp(value, low, high) {
  return value < low ? low : value > high ? high : value;
}

function setupCanvas(canvas) {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext('2d');
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return { context, width, height };
}

function drawHistogram(counts) {
  const { context, width, height } = setupCanvas(histogramCanvas);
  const left = 52;
  const right = 18;
  const top = 18;
  const bottom = 46;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const peak = Math.max(...counts, 1);
  const slot = plotWidth / counts.length;
  const barWidth = slot * 0.68;

  context.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';

  for (let step = 0; step <= 4; step++) {
    const y = top + plotHeight - (plotHeight * step) / 4;
    context.strokeStyle = '#263041';
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(left, y);
    context.lineTo(width - right, y);
    context.stroke();
    context.fillStyle = '#8b949e';
    context.textAlign = 'right';
    context.fillText(String(Math.round((peak * step) / 4)), left - 8, y + 4);
  }

  const gradient = context.createLinearGradient(0, top, 0, top + plotHeight);
  gradient.addColorStop(0, '#58a6ff');
  gradient.addColorStop(1, '#1f6feb');

  for (let index = 0; index < counts.length; index++) {
    const barHeight = (plotHeight * counts[index]) / peak;
    const x = left + slot * index + (slot - barWidth) / 2;
    const y = top + plotHeight - barHeight;
    context.fillStyle = gradient;
    context.beginPath();
    context.roundRect(x, y, barWidth, Math.max(barHeight, 1), 3);
    context.fill();

    context.fillStyle = '#e6edf3';
    context.textAlign = 'center';
    context.fillText(String(counts[index]), x + barWidth / 2, y - 6);

    context.fillStyle = '#8b949e';
    context.fillText(`${index * 10}–${(index + 1) * 10}%`, x + barWidth / 2, top + plotHeight + 18);
  }

  context.strokeStyle = '#3d4a5c';
  context.beginPath();
  context.moveTo(left, top + plotHeight);
  context.lineTo(width - right, top + plotHeight);
  context.stroke();
}

function drawStrip() {
  const { context, width, height } = setupCanvas(stripCanvas);
  const left = 56;
  const right = 18;
  const top = 18;
  const bottom = 34;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const floor = 0.05;
  const logFloor = Math.log10(floor);

  function yOf(value) {
    const clamped = Math.max(value, floor);
    return top + plotHeight - (plotHeight * (Math.log10(clamped) - logFloor)) / (0 - logFloor);
  }

  context.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
  const ticks = [
    [0.05, '5%'],
    [0.1, '10%'],
    [0.25, '25%'],
    [0.5, '50%'],
    [1, '100%'],
  ];
  for (const [value, label] of ticks) {
    const y = yOf(value);
    context.strokeStyle = value === 0.5 ? '#f0883e' : '#263041';
    context.setLineDash(value === 0.5 ? [5, 4] : []);
    context.beginPath();
    context.moveTo(left, y);
    context.lineTo(width - right, y);
    context.stroke();
    context.setLineDash([]);
    context.fillStyle = '#8b949e';
    context.textAlign = 'right';
    context.fillText(label, left - 8, y + 4);
  }

  const columns = Math.max(60, Math.floor(plotWidth / 3));
  const step = probabilities.length / columns;
  const barWidth = plotWidth / columns;
  for (let column = 0; column < columns; column++) {
    const index = Math.min(probabilities.length - 1, Math.floor(column * step));
    if (index < 0) break;
    const value = probabilities[index];
    const y = yOf(value);
    const x = left + column * barWidth;
    context.fillStyle = value >= 0.75 ? '#58a6ff' : value >= 0.25 ? '#3fb950' : '#8b949e';
    context.globalAlpha = 0.9;
    context.fillRect(x, y, Math.max(barWidth, 0.6), top + plotHeight - y);
  }
  context.globalAlpha = 1;

  context.strokeStyle = '#3d4a5c';
  context.beginPath();
  context.moveTo(left, top + plotHeight);
  context.lineTo(width - right, top + plotHeight);
  context.stroke();

  context.fillStyle = '#8b949e';
  context.textAlign = 'right';
  context.fillText(`共 ${edgeCount.toLocaleString()} 条，按概率降序`, width - right, height - 10);
}

function renderCards(stats) {
  const cards = [
    ['节点数', nodeCount.toLocaleString()],
    ['连接数', edgeCount.toLocaleString()],
    ['有出边节点', sourceCount.toLocaleString()],
    ['中位概率', `${(stats.median * 100).toFixed(1)}%`],
    ['概率 = 100%', stats.full.toLocaleString()],
    ['概率 < 25%', stats.low.toLocaleString()],
  ];
  cardsEl.innerHTML = cards
    .map(([label, value]) => `<div class="card"><span>${label}</span><b>${value}</b></div>`)
    .join('');
}

function renderTop() {
  if (!topEdges.length) {
    topEl.innerHTML = '<div class="sub">没有连接</div>';
    return;
  }
  topEl.innerHTML = `<table>${topEdges
    .map(([source, target, weight]) => {
      const probability = clamp(weight, 0, 1);
      return `<tr><td>#${source} → #${target}</td><td>权重 ${weight.toFixed(3)} · 概率 ${(probability * 100).toFixed(1)}%</td></tr>`;
    })
    .join('')}</table>`;
}

async function start() {
  const response = await fetch('/api/probability', { cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json();
  if (payload.error) throw new Error(payload.error);
  if (payload.missing) {
    metaEl.textContent = `未找到数据库：${payload.database || 'nodes.db'}`;
    emptyEl.hidden = false;
    return;
  }

  const nodes = payload.nodes || [];
  const edges = payload.edges || [];
  nodeCount = nodes.length;
  edgeCount = edges.length;

  const sources = new Set();
  const values = new Float64Array(edgeCount);
  for (let i = 0; i < edgeCount; i++) {
    const [source, target, weight] = edges[i];
    sources.add(source);
    values[i] = clamp(Number(weight) || 0, 0, 1);
  }
  sourceCount = sources.size;
  probabilities = Array.from(values).sort((a, b) => b - a);

  const generated = payload.generatedAt ? new Date(payload.generatedAt).toLocaleTimeString() : '';
  metaEl.textContent = `${nodeCount.toLocaleString()} 节点 · ${edgeCount.toLocaleString()} 连接 · 读取于 ${generated}`;

  if (edgeCount === 0) {
    emptyEl.hidden = false;
    renderCards({ median: 0, full: 0, low: 0 });
    renderTop();
    drawHistogram(new Array(10).fill(0));
    drawStrip();
    return;
  }

  emptyEl.hidden = true;
  const counts = new Array(10).fill(0);
  let full = 0;
  let low = 0;
  for (const value of probabilities) {
    counts[Math.min(9, Math.floor(value * 10))] += 1;
    if (value >= 0.999) full += 1;
    if (value < 0.25) low += 1;
  }
  const median = probabilities[Math.floor(probabilities.length / 2)] || 0;

  renderCards({ median, full, low });
  topEdges = edges
    .slice()
    .sort((a, b) => (Number(b[2]) || 0) - (Number(a[2]) || 0))
    .slice(0, 10);
  renderTop();
  drawHistogram(counts);
  drawStrip();
}

window.addEventListener('resize', () => {
  if (probabilities.length === 0 && edgeCount === 0) return;
  const counts = new Array(10).fill(0);
  for (const value of probabilities) {
    counts[Math.min(9, Math.floor(value * 10))] += 1;
  }
  drawHistogram(counts);
  drawStrip();
});

start().catch((error) => {
  metaEl.textContent = `读取数据库失败：${error.message}`;
});
