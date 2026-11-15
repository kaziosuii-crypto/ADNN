'use strict';

// 两个独立页面：三维网络图（8421）与连接概率（8422）。
// 每次访问页面时读取一次 nodes.db，页面数据不做任何硬编码。
// 数据来源已切到 SMCGP14 的 smcgp_nodes 表（每行 id + graph JSON）。

const fs = require('fs');
const http = require('http');
const path = require('path');
const { DatabaseSync } = require('node:sqlite');

const ROOT = path.resolve(__dirname, '..');
const PUBLIC_DIR = path.join(__dirname, 'public');
const DB_PATH = path.resolve(ROOT, process.env.BTB_DB || 'nodes.db');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
};

// 神经元按 ID 区间分类（与 demo/pong/pong_adapter.py 的分组一致）：
// 0–3999 视觉；4000–7199 操作/感觉四组；10000–19999 刺激神经元；其余为路由扩张出来的神经元。
const NEURON_TYPES = [
  { key: 'visual', label: '视觉', color: '#58a6ff' },
  { key: 'group', label: '操作/感觉', color: '#3fb950' },
  { key: 'stim', label: '刺激', color: '#d2a8ff' },
  { key: 'expanded', label: '扩张', color: '#e3b341' },
];

/** 按 ID 判断神经元类型下标（对应 NEURON_TYPES 的顺序）。 */
function neuronType(id) {
  if (id >= 0 && id < 4000) return 0;
  if (id >= 4000 && id < 7200) return 1;
  if (id >= 10000 && id < 20000) return 2;
  return 3;
}

// 三维页数据缓存：数据库文件未变化时直接复用（统计入度/出度要扫全部基因，比较重）。
let graphCache = null;

/**
 * 读取 SMCGP14 节点表，返回三维页面用的神经元列表。
 * 三维页展示的是“神经元”本身（不是基因）：每个元素是 [id, 类型下标]。
 * 神经元内部是一张 SMCGP 图，点“查看 SMCGP”时才去读它的基因。
 */
function readGraph() {
  if (!fs.existsSync(DB_PATH)) {
    return { nodes: [], edges: [], metric: 'type', types: NEURON_TYPES,
             missing: true, database: DB_PATH };
  }
  const stat = fs.statSync(DB_PATH);
  const cacheKey = `${stat.mtimeMs}:${stat.size}`;
  if (graphCache && graphCache.key === cacheKey) {
    return graphCache.payload;
  }

  const database = new DatabaseSync(DB_PATH, { readOnly: true });
  try {
    // 一次扫描统计每个神经元的：基因数、INP 数（入度）、OUTPUT 数（出度）。
    // 入度 = 这张图里 INP 基因的个数（能接收的输入槽数量）；
    // 出度 = 这张图里 OUTPUT 基因的个数（每轮最多发出的消息条数）。
    const rows = database.prepare(`
      SELECT n.id AS id,
             CASE WHEN json_valid(n.graph) THEN json_array_length(n.graph) ELSE 0 END AS genes,
             SUM(CASE WHEN CAST(json_extract(j.value, '$.op') AS INTEGER) = 0 THEN 1 ELSE 0 END) AS ins,
             SUM(CASE WHEN CAST(json_extract(j.value, '$.op') AS INTEGER) = 1 THEN 1 ELSE 0 END) AS outs
      FROM smcgp_nodes AS n
      LEFT JOIN json_each(n.graph) AS j
      WHERE json_valid(n.graph)
      GROUP BY n.id`).all();
    const nodes = [];
    for (const row of rows) {
      const id = Number(row.id);
      nodes.push([
        id,
        neuronType(id),
        Number(row.genes) || 0,
        Number(row.ins) || 0,
        Number(row.outs) || 0,
      ]);
    }
    nodes.sort((a, b) => a[0] - b[0]);
    const payload = { nodes, edges: [], metric: 'type', types: NEURON_TYPES, database: DB_PATH };
    graphCache = { key: cacheKey, payload };
    return payload;
  } finally {
    database.close();
  }
}

/**
 * 读取指定 ID 的 SMCGP 图（基因数组），供“查看 SMCGP”页面使用。
 * 返回 { id, genes, gene_count }；节点不存在时返回 { missing: true }。
 */
function readSmcgpNode(rawId) {
  const id = Number.parseInt(rawId, 10);
  if (!Number.isFinite(id)) {
    return { error: 'id 参数不是合法整数' };
  }
  if (!fs.existsSync(DB_PATH)) {
    return { missing: true, id, database: DB_PATH };
  }
  const database = new DatabaseSync(DB_PATH, { readOnly: true });
  try {
    const row = database.prepare('SELECT id, graph FROM smcgp_nodes WHERE id = ?').get(id);
    if (!row) {
      return { missing: true, id, database: DB_PATH };
    }
    const genes = JSON.parse(row.graph || '[]');
    return { id: Number(row.id), genes, gene_count: genes.length, database: DB_PATH };
  } finally {
    database.close();
  }
}

// SMCGP 结构统计缓存：数据库文件未变化时直接返回上次结果，避免每次刷新都全表扫描。
let smcgpStatsCache = null;

/**
 * 统计 SMCGP 网络结构（8422 页面用）。
 * 内容包括：神经元/基因总量、神经元类型分布、14 种指令分布、
 * 相对连接距离分布（c0/c1）、基因数分箱、最大神经元 Top10。
 */
function readSmcgpStats() {
  if (!fs.existsSync(DB_PATH)) {
    return { missing: true, database: DB_PATH };
  }
  const stat = fs.statSync(DB_PATH);
  const cacheKey = `${stat.mtimeMs}:${stat.size}`;
  if (smcgpStatsCache && smcgpStatsCache.key === cacheKey) {
    return smcgpStatsCache.payload;
  }

  const database = new DatabaseSync(DB_PATH, { readOnly: true });
  try {
    const summary = database.prepare(`
      SELECT COUNT(*) AS nodes,
             IFNULL(SUM(CASE WHEN json_valid(graph) THEN json_array_length(graph) ELSE 0 END), 0) AS genes,
             IFNULL(MAX(CASE WHEN json_valid(graph) THEN json_array_length(graph) ELSE 0 END), 0) AS max_genes
      FROM smcgp_nodes`).get();

    const typeCounts = database.prepare(`
      SELECT
        SUM(CASE WHEN id >= 0 AND id < 4000 THEN 1 ELSE 0 END) AS visual,
        SUM(CASE WHEN id >= 4000 AND id < 7200 THEN 1 ELSE 0 END) AS group_cnt,
        SUM(CASE WHEN id >= 10000 AND id < 20000 THEN 1 ELSE 0 END) AS stim,
        SUM(CASE WHEN (id >= 7200 AND id < 10000) OR id >= 20000 THEN 1 ELSE 0 END) AS expanded
      FROM smcgp_nodes`).get();

    // 14 种指令分布：一次 json_each 扫描 + GROUP BY。
    const opRows = database.prepare(`
      SELECT CAST(json_extract(j.value, '$.op') AS INTEGER) AS op, COUNT(*) AS count
      FROM smcgp_nodes, json_each(smcgp_nodes.graph) AS j
      WHERE json_valid(smcgp_nodes.graph)
      GROUP BY op`).all();
    const opCounts = new Array(14).fill(0);
    for (const row of opRows) {
      const op = Number(row.op);
      if (op >= 0 && op < 14) opCounts[op] += Number(row.count);
    }

    // 相对连接距离：c0、c1 各扫一遍，合并成 1..16 与 >16 的分布。
    const distanceCounts = new Map();
    for (const field of ['$.c0', '$.c1']) {
      const rows = database.prepare(`
        SELECT CAST(json_extract(j.value, '${field}') AS INTEGER) AS distance, COUNT(*) AS count
        FROM smcgp_nodes, json_each(smcgp_nodes.graph) AS j
        WHERE json_valid(smcgp_nodes.graph)
        GROUP BY distance`).all();
      for (const row of rows) {
        const distance = Number(row.distance);
        const key = Number.isFinite(distance) && distance >= 1 ? distance : 0;
        distanceCounts.set(key, (distanceCounts.get(key) || 0) + Number(row.count));
      }
    }
    const linkDistances = [];
    for (let distance = 1; distance <= 16; distance++) {
      linkDistances.push([distance, distanceCounts.get(distance) || 0]);
    }
    let beyond = 0;
    for (const [distance, count] of distanceCounts) {
      if (distance > 16) beyond += count;
    }
    linkDistances.push(['>16', beyond]);
    linkDistances.push([0, distanceCounts.get(0) || 0]); // 0 = 不连接（读 0）

    // 基因数分箱（每个神经元一条）。
    const sizeBins = database.prepare(`
      SELECT CASE
        WHEN g < 8 THEN '0-7'
        WHEN g < 16 THEN '8-15'
        WHEN g < 32 THEN '16-31'
        WHEN g < 64 THEN '32-63'
        WHEN g < 128 THEN '64-127'
        WHEN g < 256 THEN '128-255'
        WHEN g < 512 THEN '256-511'
        WHEN g < 1024 THEN '512-1023'
        WHEN g < 4096 THEN '1K-4K'
        WHEN g < 16384 THEN '4K-16K'
        ELSE '16K+'
      END AS label, COUNT(*) AS count
      FROM (SELECT json_array_length(graph) AS g FROM smcgp_nodes WHERE json_valid(graph))
      GROUP BY label`).all();

    const top = database.prepare(`
      SELECT id, json_array_length(graph) AS genes FROM smcgp_nodes
      WHERE json_valid(graph)
      ORDER BY genes DESC LIMIT 10`).all()
      .map((row) => [Number(row.id), Number(row.genes)]);

    const payload = {
      database: DB_PATH,
      nodes: Number(summary.nodes) || 0,
      genes: Number(summary.genes) || 0,
      maxGenes: Number(summary.max_genes) || 0,
      types: NEURON_TYPES.map((entry, index) => ({
        ...entry,
        count: Number([
          typeCounts.visual, typeCounts.group_cnt, typeCounts.stim, typeCounts.expanded,
        ][index]) || 0,
      })),
      ops: opCounts,
      linkDistances,
      sizeBins: sizeBins.map((row) => [String(row.label), Number(row.count)]),
      top,
    };
    smcgpStatsCache = { key: cacheKey, payload };
    return payload;
  } finally {
    database.close();
  }
}


/** 把 public 目录下的静态文件发出去（带 no-cache，保证改完页面刷新即生效）。 */
function serveFile(response, filePath) {
  if (!filePath.startsWith(PUBLIC_DIR) || !fs.existsSync(filePath)) {
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end('Not found');
    return;
  }
  response.writeHead(200, {
    'Content-Type': MIME[path.extname(filePath)] || 'application/octet-stream',
    'Cache-Control': 'no-cache',
  });
  fs.createReadStream(filePath).pipe(response);
}

function createViewServer(port, page, apiPath, label, builder) {
  const buildPayload = typeof builder === 'function' ? builder : readGraph;
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);

    if (url.pathname === apiPath) {
      try {
        const payload = buildPayload();
        payload.generatedAt = new Date().toISOString();
        response.writeHead(200, {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
        });
        response.end(JSON.stringify(payload));
      } catch (error) {
        response.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
        response.end(JSON.stringify({ error: error.message }));
      }
      return;
    }

    // 单个神经元的 SMCGP 图（“查看 SMCGP”页面用）。
    if (url.pathname === '/api/smcgp') {
      try {
        const payload = readSmcgpNode(url.searchParams.get('id') || '');
        payload.generatedAt = new Date().toISOString();
        response.writeHead(200, {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
        });
        response.end(JSON.stringify(payload));
      } catch (error) {
        response.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
        response.end(JSON.stringify({ error: error.message }));
      }
      return;
    }

    if (url.pathname === '/favicon.ico') {
      response.writeHead(204);
      response.end();
      return;
    }

    const relative = url.pathname === '/' ? page : url.pathname.replace(/^\/+/, '');
    serveFile(response, path.join(PUBLIC_DIR, relative));
  });

  server.listen(port, () => {
    console.log(`${label}: http://localhost:${port}/`);
  });
  return server;
}

function startViewServers() {
  const graphPort = Number(process.env.BTB_GRAPH_PORT || 8421);
  const probabilityPort = Number(process.env.BTB_PROB_PORT || 8422);
  return [
    createViewServer(graphPort, 'graph.html', '/api/graph', '三维网络图', readGraph),
    createViewServer(probabilityPort, 'probability.html', '/api/probability', 'SMCGP 结构统计', readSmcgpStats),
  ];
}

module.exports = { startViewServers };
