'use strict';

// 两个独立页面：三维网络图（8421）与连接概率（8422）。
// 每次访问页面时读取一次 nodes.db，页面数据不做任何硬编码。

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

function readGraph() {
  if (!fs.existsSync(DB_PATH)) {
    return { nodes: [], edges: [], missing: true, database: DB_PATH };
  }
  const database = new DatabaseSync(DB_PATH, { readOnly: true });
  try {
    const rows = database.prepare('SELECT id, value, forwards FROM nodes').all();
    const nodes = [];
    const edges = [];
    for (const row of rows) {
      nodes.push([row.id, row.value]);
      if (!row.forwards || row.forwards === '{}') continue;
      let forwards;
      try {
        forwards = JSON.parse(row.forwards);
      } catch (error) {
        continue;
      }
      for (const target in forwards) {
        const weight = forwards[target];
        if (typeof weight !== 'number') continue;
        edges.push([row.id, Number(target), weight]);
      }
    }
    return { nodes, edges, database: DB_PATH };
  } finally {
    database.close();
  }
}

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

function createViewServer(port, page, apiPath, label) {
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);

    if (url.pathname === apiPath) {
      try {
        const payload = readGraph();
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
    createViewServer(graphPort, 'graph.html', '/api/graph', '三维网络图'),
    createViewServer(probabilityPort, 'probability.html', '/api/probability', '连接概率'),
  ];
}

module.exports = { startViewServers };
