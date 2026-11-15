'use strict';

// 单个神经元的 SMCGP 图查看页：从 /api/smcgp?id=NNN 读取该神经元的基因数组，
// 按“基因下标”排成网格，画出每个基因指向前面基因的两条相对连接（i - c0 / i - c1）。
// 基因可达十万级，所以只画一个窗口（起始下标 + 显示数量），并可按 OUTPUT 可达过滤。

const canvas = document.getElementById('scene');
const context = canvas.getContext('2d');
const messageEl = document.getElementById('message');
const statsEl = document.getElementById('stats');
const detailEl = document.getElementById('detail');
const tooltipEl = document.getElementById('tooltip');
const opsEl = document.getElementById('ops');
const titleEl = document.getElementById('title');
const startInput = document.getElementById('start');
const countSelect = document.getElementById('count');
const onlyReachableInput = document.getElementById('only-reachable');
const hideConstInput = document.getElementById('hide-const');
const gotoInput = document.getElementById('goto');

// 14 种 SMCGP14 指令的名称与颜色（与 compute/node.py 的 Op 一致）。
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

let nodeId = null;
let genes = [];
let reachable = new Uint8Array(0);
let windowIndices = [];
let layout = [];
let positionOf = new Map();
let hovered = -1;
let selected = -1;
let start = 0;
let count = 1500;

function opName(op) {
  return OPS[op] ? OPS[op].name : `OP${op}`;
}

function opColor(op) {
  return OPS[op] ? OPS[op].color : '#8b949e';
}

/** 显示/隐藏全屏提示（读取失败、节点不存在等）。 */
function showMessage(text) {
  messageEl.textContent = text;
  messageEl.classList.remove('hidden');
}

function hideMessage() {
  messageEl.classList.add('hidden');
}

/**
 * 从所有 OUTPUT 基因反向遍历，标记“本轮会被求值到”的基因下标。
 * 链接一律指向更小的下标，所以栈遍历不会死循环（越界/前向连接直接跳过）。
 */
function computeReachable() {
  const total = genes.length;
  reachable = new Uint8Array(total);
  const stack = [];
  for (let i = 0; i < total; i++) {
    if (genes[i].op === 1) stack.push(i);
  }
  while (stack.length > 0) {
    const index = stack.pop();
    if (index < 0 || index >= total || reachable[index]) continue;
    reachable[index] = 1;
    const gene = genes[index];
    if (gene.c0 >= 1) stack.push(index - gene.c0);
    if (gene.c1 >= 1) stack.push(index - gene.c1);
  }
}

/** 统计指令分布，渲染左上角的图例。 */
function renderOpsLegend() {
  const counts = new Array(OPS.length).fill(0);
  for (const gene of genes) {
    const op = Number(gene.op);
    if (op >= 0 && op < counts.length) counts[op] += 1;
  }
  opsEl.innerHTML = OPS.map((entry, op) => (
    `<span><i style="background:${entry.color}"></i>${entry.name} ${counts[op]}</span>`
  )).join('');
}

/**
 * 按当前筛选与窗口重建要绘制的基因列表，并把它们排成网格。
 * 布局信息缓存到 layout / positionOf，供绘制与点击命中使用。
 */
function rebuildLayout() {
  const total = genes.length;
  const onlyReachable = onlyReachableInput.checked;
  const hideConst = hideConstInput.checked;
  start = Math.max(0, Math.min(start, Math.max(0, total - 1)));
  startInput.value = String(start);
  const end = Math.min(total, start + count);

  windowIndices = [];
  for (let index = start; index < end; index++) {
    if (onlyReachable && !reachable[index]) continue;
    if (hideConst && Number(genes[index].op) === 4) continue;
    windowIndices.push(index);
  }

  layout = [];
  positionOf = new Map();
  if (windowIndices.length === 0) return;

  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const marginX = 24;
  const marginY = 70;
  const usableWidth = Math.max(120, width - marginX * 2);
  const usableHeight = Math.max(120, height - marginY - 24);
  // 网格列数按“接近正方”的密度估算，避免节点被压成一条线。
  const columns = Math.max(4, Math.min(220, Math.ceil(Math.sqrt(windowIndices.length * 1.9))));
  const rows = Math.ceil(windowIndices.length / columns);
  const cellWidth = usableWidth / columns;
  const cellHeight = usableHeight / Math.max(rows, 1);
  const size = Math.max(3, Math.min(9, Math.min(cellWidth, cellHeight) * 0.62));

  for (let k = 0; k < windowIndices.length; k++) {
    const column = k % columns;
    const row = Math.floor(k / columns);
    const x = marginX + cellWidth * (column + 0.5);
    const y = marginY + cellHeight * (row + 0.5);
    const item = { index: windowIndices[k], x, y, size };
    layout.push(item);
    positionOf.set(item.index, item);
  }
}

/** 画两条连接线：目标在窗口内就画连线，不在窗口内就画一个“向左的外部依赖”小箭头。 */
function drawLinks() {
  context.lineWidth = 1;
  for (const item of layout) {
    const gene = genes[item.index];
    const links = [gene.c0, gene.c1];
    for (const link of links) {
      if (link < 1) continue;
      const dependency = item.index - link;
      const source = positionOf.get(dependency);
      context.strokeStyle = opColor(Number(gene.op));
      context.globalAlpha = reachable[item.index] ? 0.5 : 0.22;
      if (source) {
        context.beginPath();
        context.moveTo(source.x, source.y);
        context.lineTo(item.x, item.y);
        context.stroke();
      } else {
        // 窗口外的依赖：在节点左侧画一个短截线，提示“它还引用了窗口外的基因”。
        context.beginPath();
        context.moveTo(item.x - item.size - 5, item.y);
        context.lineTo(item.x - item.size - 1, item.y);
        context.stroke();
      }
    }
  }
  context.globalAlpha = 1;
}

/** 绘制窗口内的基因节点：颜色 = 指令类型；OUTPUT 带金环，未命中可达的节点虚化。 */
function drawNodes() {
  for (const item of layout) {
    const gene = genes[item.index];
    const op = Number(gene.op);
    const isReachable = reachable[item.index] === 1;
    context.globalAlpha = isReachable ? 1 : 0.35;
    context.fillStyle = opColor(op);
    context.fillRect(item.x - item.size / 2, item.y - item.size / 2, item.size, item.size);
    if (op === 1) {
      context.strokeStyle = '#f0b429';
      context.lineWidth = 1.6;
      context.strokeRect(item.x - item.size / 2 - 2, item.y - item.size / 2 - 2,
                         item.size + 4, item.size + 4);
    }
  }
  context.globalAlpha = 1;

  if (selected >= 0 && positionOf.has(selected)) {
    const item = positionOf.get(selected);
    context.strokeStyle = '#e6edf3';
    context.lineWidth = 1.6;
    context.beginPath();
    context.arc(item.x, item.y, item.size + 5, 0, Math.PI * 2);
    context.stroke();
  }
  if (hovered >= 0 && hovered !== selected && positionOf.has(hovered)) {
    const item = positionOf.get(hovered);
    context.strokeStyle = 'rgba(230, 237, 243, 0.7)';
    context.lineWidth = 1.1;
    context.beginPath();
    context.arc(item.x, item.y, item.size + 4, 0, Math.PI * 2);
    context.stroke();
  }
}

function render() {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  drawLinks();
  drawNodes();
}

function rebuildAndRender() {
  rebuildLayout();
  render();
  updateStats();
}

function updateStats() {
  const total = genes.length;
  const shown = windowIndices.length;
  const reachableCount = reachable.reduce((sum, flag) => sum + flag, 0);
  const end = Math.min(total, start + count);
  statsEl.textContent = `神经元 #${nodeId} · 基因 ${total.toLocaleString()} · OUTPUT 可达 ${reachableCount.toLocaleString()}`
    + ` · 窗口 [${start.toLocaleString()}, ${end.toLocaleString()}) 显示 ${shown.toLocaleString()}`;
}

/** 命中测试：返回鼠标附近（12px 内）最近的基因下标，没有则 -1。 */
function pick(clientX, clientY) {
  let best = -1;
  let bestDistance = 12 * 12;
  for (const item of layout) {
    const dx = item.x - clientX;
    const dy = item.y - clientY;
    const distance = dx * dx + dy * dy;
    if (distance < bestDistance) {
      best = item.index;
      bestDistance = distance;
    }
  }
  return best;
}

function showTooltip(index, clientX, clientY) {
  const gene = genes[index];
  tooltipEl.style.display = 'block';
  tooltipEl.style.left = `${clientX + 14}px`;
  tooltipEl.style.top = `${clientY + 14}px`;
  tooltipEl.textContent = `#${index} ${opName(Number(gene.op))} · c0=${gene.c0} c1=${gene.c1}`;
}

function hideTooltip() {
  tooltipEl.style.display = 'none';
}

/**
 * 选中某个基因：右侧显示指令、参数、两条相对连接的实际依赖下标，
 * 依赖下标可点击跳转到对应窗口。
 */
function showDetail(index) {
  const gene = genes[index];
  const op = Number(gene.op);
  const deps = [];
  for (const [label, link] of [['c0', gene.c0], ['c1', gene.c1]]) {
    if (link < 1) {
      deps.push(`<tr><td>${label}</td><td>${link}（不连接）</td></tr>`);
      continue;
    }
    const dependency = index - link;
    const valid = dependency >= 0 && dependency < genes.length;
    const cell = valid
      ? `<span class="link" data-jump="${dependency}">#${dependency}</span>`
      : '越界读 0';
    deps.push(`<tr><td>${label} = ${link}</td><td>${cell}</td></tr>`);
  }
  detailEl.innerHTML = `
    <strong>基因 #${index}</strong><br>
    指令 <code>${opName(op)}</code> · 可达 ${reachable[index] ? '是' : '否'}
    <table>
      ${deps.join('')}
      <tr><td>p0</td><td>${Number(gene.p0).toFixed(4)}</td></tr>
      <tr><td>p1</td><td>${Number(gene.p1).toFixed(4)}</td></tr>
      <tr><td>p2</td><td>${Number(gene.p2).toFixed(4)}</td></tr>
    </table>
    <div style="margin-top:6px">窗口 [${start.toLocaleString()}, ${Math.min(genes.length, start + count).toLocaleString()})</div>
  `;
  for (const link of detailEl.querySelectorAll('[data-jump]')) {
    link.addEventListener('click', () => {
      const target = Number(link.getAttribute('data-jump'));
      jumpTo(target);
    });
  }
}

/** 把窗口移动到目标下标附近并重新渲染。 */
function jumpTo(index) {
  start = Math.max(0, index - Math.floor(count / 2));
  selected = index;
  hovered = -1;
  hideTooltip();
  rebuildAndRender();
  if (positionOf.has(index)) showDetail(index);
}

canvas.addEventListener('pointermove', (event) => {
  const index = pick(event.clientX, event.clientY);
  if (index !== hovered) {
    hovered = index;
    render();
  }
  if (index >= 0) showTooltip(index, event.clientX, event.clientY);
  else hideTooltip();
});

canvas.addEventListener('pointerleave', () => {
  hovered = -1;
  hideTooltip();
  render();
});

canvas.addEventListener('click', (event) => {
  const index = pick(event.clientX, event.clientY);
  selected = index;
  if (index >= 0) {
    showDetail(index);
  } else {
    detailEl.textContent = '点击基因节点查看指令与参数';
  }
  render();
});

window.addEventListener('resize', () => {
  rebuildAndRender();
});

startInput.addEventListener('change', () => {
  start = Math.max(0, Number(startInput.value) || 0);
  rebuildAndRender();
});

countSelect.addEventListener('change', () => {
  count = Number(countSelect.value) || 1500;
  rebuildAndRender();
});

onlyReachableInput.addEventListener('change', rebuildAndRender);
hideConstInput.addEventListener('change', rebuildAndRender);

document.getElementById('apply').addEventListener('click', rebuildAndRender);
document.getElementById('last').addEventListener('click', () => {
  start = Math.max(0, genes.length - count);
  rebuildAndRender();
});

function goToIndex() {
  const value = Number(gotoInput.value);
  if (!Number.isFinite(value) || value < 0) return;
  jumpTo(Math.min(Math.floor(value), genes.length - 1));
}

document.getElementById('goto-btn').addEventListener('click', goToIndex);
gotoInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') goToIndex();
});

/** 读取 URL 里的 id 参数，拉取该神经元的 SMCGP 图并初始化页面。 */
function boot() {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get('id');
  nodeId = raw === null ? null : Number.parseInt(raw, 10);
  if (nodeId === null || !Number.isFinite(nodeId)) {
    showMessage('缺少或非法的 id 参数，例如 /smcgp.html?id=3872');
    return;
  }
  titleEl.textContent = `ADNN · SMCGP 图 · 神经元 #${nodeId}`;
  fetch(`/api/smcgp?id=${nodeId}`, { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then((payload) => {
      if (payload.error) {
        showMessage(`读取失败：${payload.error}`);
        return;
      }
      if (payload.missing) {
        showMessage(`数据库里没有神经元 #${nodeId}（数据库：${payload.database || 'nodes.db'}）`);
        return;
      }
      genes = Array.isArray(payload.genes) ? payload.genes : [];
      if (genes.length === 0) {
        showMessage(`神经元 #${nodeId} 的图是空的`);
        return;
      }
      hideMessage();
      computeReachable();
      renderOpsLegend();
      count = Number(countSelect.value) || 1500;
      // 默认看末尾：OUTPUT 相关基因通常集中在图的尾部。
      start = Math.max(0, genes.length - count);
      rebuildAndRender();
    })
    .catch((error) => {
      showMessage(`读取失败：${error.message}`);
    });
}

boot();
