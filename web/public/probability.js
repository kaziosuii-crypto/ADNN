'use strict';

// 8422：SMCGP 结构统计页。
// 从 /api/probability 读取 smcgp_nodes 的聚合统计（指令分布、连接距离、基因数分布等），
// 全部由服务端用 SQLite JSON1 聚合，页面只负责画图。

const cardsEl = document.getElementById('cards');
const metaEl = document.getElementById('meta');
const emptyEl = document.getElementById('empty');
const typesEl = document.getElementById('types');
const topEl = document.getElementById('top');
const opsCanvas = document.getElementById('ops');
const sizesCanvas = document.getElementById('sizes');
const distancesCanvas = document.getElementById('distances');

// 与 smcgp 查看页一致的 14 种指令名称与颜色。
const OPS = [
  { name: 'INP', color: '#58a6ff' },
  { name: 'OUTPUT', color: '#f0b429' },
  { name: 'DADD', color: '#3fb950' },
  { name: 'DMULT', color: '#56d364' },
  { name: 'CONST', color: '#8b949e' },
  { name: 'IFLTE', color: '#d2a8ff' },
  { name: 'INDX', color: '#79c0ff' },
  { name: 'ADD', color: '#ff7b72' },
  { name: 'DEL', color: '#ffa198' },
  { name: 'MOV', color: '#ff9e64' },
  { name: 'CHC', color: '#e3b341' },
  { name: 'CHF', color: '#d29922' },
  { name: 'FLR', color: '#f778ba' },
  { name: 'CHP', color: '#db6d28' },
];

let payload = null;

/** 按设备像素比准备画布，返回绘图上下文与 CSS 尺寸。 */
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

/** 大数字紧凑显示：12000 -> 12K。 */
function compact(value) {
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}G`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e4) return `${(value / 1e3).toFixed(1)}K`;
  return String(value);
}

/**
 * 通用柱状图：labels 为横轴标签，values 为数值，colors 为每根柱子的颜色。
 * options.log = true 时纵轴用对数刻度（指令量级差异很大时用）。
 */
function drawBars(canvas, labels, values, colors, options = {}) {
  const { context, width, height } = setupCanvas(canvas);
  const left = 64;
  const right = 18;
  const top = 22;
  const bottom = 46;
  const plotWidth = Math.max(60, width - left - right);
  const plotHeight = Math.max(60, height - top - bottom);
  const max = Math.max(...values, 1);
  const useLog = Boolean(options.log);
  const scale = (value) => {
    if (useLog) {
      return Math.log10(value + 1) / Math.log10(max + 1);
    }
    return value / max;
  };

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
    const value = useLog ? Math.pow(max + 1, step / 4) - 1 : (max * step) / 4;
    context.fillText(compact(Math.round(value)), left - 8, y + 4);
  }

  const slot = plotWidth / Math.max(labels.length, 1);
  const barWidth = Math.max(3, slot * 0.68);
  for (let index = 0; index < labels.length; index++) {
    const barHeight = plotHeight * scale(values[index]);
    const x = left + slot * index + (slot - barWidth) / 2;
    const y = top + plotHeight - barHeight;
    context.fillStyle = colors[index] || '#58a6ff';
    context.globalAlpha = values[index] > 0 ? 0.95 : 0.25;
    context.beginPath();
    context.roundRect(x, y, barWidth, Math.max(barHeight, 1), 3);
    context.fill();
    context.globalAlpha = 1;

    if (values[index] > 0) {
      context.fillStyle = '#e6edf3';
      context.textAlign = 'center';
      context.fillText(compact(values[index]), x + barWidth / 2, y - 6);
    }

    context.fillStyle = '#8b949e';
    context.textAlign = 'center';
    context.fillText(String(labels[index]), x + barWidth / 2, top + plotHeight + 18);
  }

  context.strokeStyle = '#3d4a5c';
  context.beginPath();
  context.moveTo(left, top + plotHeight);
  context.lineTo(width - right, top + plotHeight);
  context.stroke();
}

function renderCards() {
  const nodes = payload.nodes || 0;
  const genes = payload.genes || 0;
  const selfModify = payload.ops.slice(7, 14).reduce((sum, value) => sum + value, 0);
  const outputs = payload.ops[1] || 0;
  const average = nodes > 0 ? genes / nodes : 0;
  const cards = [
    ['神经元数', nodes.toLocaleString()],
    ['基因总数', genes.toLocaleString()],
    ['平均基因/神经元', average.toFixed(1)],
    ['最大基因数', (payload.maxGenes || 0).toLocaleString()],
    ['自修改指令', selfModify.toLocaleString()],
    ['OUTPUT 指令', outputs.toLocaleString()],
  ];
  cardsEl.innerHTML = cards
    .map(([label, value]) => `<div class="card"><span>${label}</span><b>${value}</b></div>`)
    .join('');
}

function renderTypes() {
  typesEl.innerHTML = (payload.types || []).map((entry) => (
    `<span class="item"><i style="background:${entry.color}"></i>${entry.label} ${entry.count.toLocaleString()}</span>`
  )).join('');
}

function renderTop() {
  const top = payload.top || [];
  if (top.length === 0) {
    topEl.innerHTML = '<div class="sub">没有神经元</div>';
    return;
  }
  topEl.innerHTML = `<table>${top.map(([id, genes]) => (
    `<tr><td><a href="/smcgp.html?id=${id}" target="_blank">#${id}</a></td>`
    + `<td>${genes.toLocaleString()} 基因</td></tr>`
  )).join('')}</table>`;
}

function drawAll() {
  drawBars(opsCanvas, OPS.map((entry) => entry.name), payload.ops,
           OPS.map((entry) => entry.color), { log: true });

  const sizeBins = payload.sizeBins || [];
  drawBars(sizesCanvas, sizeBins.map((row) => row[0]), sizeBins.map((row) => row[1]),
           sizeBins.map(() => '#58a6ff'));

  const distances = payload.linkDistances || [];
  const labels = distances.map((row) => (row[0] === 0 ? '不连接' : String(row[0])));
  const values = distances.map((row) => row[1]);
  const colors = distances.map((row) => (row[0] === 0 ? '#8b949e' : row[0] === '>16' ? '#e3b341' : '#3fb950'));
  drawBars(distancesCanvas, labels, values, colors, { log: true });
}

async function start() {
  const response = await fetch('/api/probability', { cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  payload = await response.json();
  if (payload.error) throw new Error(payload.error);
  if (payload.missing) {
    metaEl.textContent = `未找到数据库：${payload.database || 'nodes.db'}`;
    emptyEl.hidden = false;
    return;
  }

  const generated = payload.generatedAt ? new Date(payload.generatedAt).toLocaleTimeString() : '';
  metaEl.textContent = `${(payload.nodes || 0).toLocaleString()} 神经元 · `
    + `${(payload.genes || 0).toLocaleString()} 基因 · 读取于 ${generated}`;

  if ((payload.nodes || 0) === 0) {
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;

  renderCards();
  renderTypes();
  renderTop();
  drawAll();
}

window.addEventListener('resize', () => {
  if (!payload || !payload.nodes) return;
  drawAll();
});

start().catch((error) => {
  metaEl.textContent = `读取数据库失败：${error.message}`;
});
