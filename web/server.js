'use strict';

// 单个网页取代原来的五个 tkinter 窗口：Pong 画面、UP / DOWN 指示灯、停止按钮、网络图。
// Node 负责静态页面与 SSE 推送，Python 子进程负责跑网络。

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { startViewServers } = require('./views');

const ROOT = path.resolve(__dirname, '..');
const PUBLIC_DIR = path.join(__dirname, 'public');
const PORT = Number(process.env.BTB_PORT || 8420);

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
};

function resolvePython() {
  if (process.env.BTB_PYTHON) return process.env.BTB_PYTHON;
  const venv = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
  return fs.existsSync(venv) ? venv : 'python';
}

const clients = new Set();
const latest = new Map(); // 类型 -> 最近一条原始 JSON 行，供新连接补发
let bridge = null;
let stopping = false;

// 只提取类型，不解析整行 JSON；状态帧包含 4000 格网格，逐帧解析会白白吃掉 CPU。
const TYPE_RE = /^\{"type":"([a-z_]+)"/i;

function send(response, line) {
  response.write(`data: ${line}\n\n`);
}

function broadcastLine(line) {
  const match = TYPE_RE.exec(line);
  if (match) latest.set(match[1], line);
  const frame = `data: ${line}\n\n`;
  for (const client of clients) {
    if (client.destroyed || client.writableEnded) {
      clients.delete(client);
      continue;
    }
    client.write(frame);
  }
}

function broadcast(message) {
  broadcastLine(JSON.stringify(message));
}

function startBridge() {
  if (bridge) return false;
  stopping = false;
  bridge = spawn(resolvePython(), ['-u', 'demo/pong/bridge.py'], {
    cwd: ROOT,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  let buffer = '';
  bridge.stdout.on('data', (chunk) => {
    buffer += chunk.toString('utf8');
    let index;
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      // 子进程已经输出合法 JSON，直接透传，不再解析后再序列化。
      if (!TYPE_RE.test(line)) {
        console.error('无法识别的子进程输出:', line.slice(0, 200));
        continue;
      }
      broadcastLine(line);
    }
  });
  bridge.stderr.on('data', (chunk) => process.stderr.write(chunk));
  bridge.on('error', (error) => {
    broadcast({ type: 'bridge_error', message: error.message });
  });
  bridge.on('close', (code) => {
    bridge = null;
    stopping = false;
    broadcast({ type: 'exit', code });
    console.log(`Python 桥接进程已退出（code=${code}）`);
  });
  latest.clear();
  broadcast({ type: 'started' });
  return true;
}

const server = http.createServer((request, response) => {
  const url = new URL(request.url, `http://${request.headers.host}`);

  if (url.pathname === '/events') {
    response.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });
    response.write(': connected\n\n');
    // 补发最近的非图状态（控制台已不再显示拓扑图）。
    for (const key of ['started', 'meta', 'state', 'stopped', 'bridge_error', 'exit']) {
      if (latest.has(key)) send(response, latest.get(key));
    }
    clients.add(response);
    request.on('close', () => clients.delete(response));
    return;
  }

  if (url.pathname === '/start' && request.method === 'POST') {
    const started = startBridge();
    response.writeHead(started ? 200 : 409, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ ok: started, running: Boolean(bridge) }));
    return;
  }

  if (url.pathname === '/stop' && request.method === 'POST') {
    if (bridge && !stopping) {
      stopping = true;
      bridge.stdin.write(`${JSON.stringify({ cmd: 'stop' })}\n`);
    }
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ ok: true }));
    return;
  }

  const relative = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/+/, '');
  const file = path.join(PUBLIC_DIR, relative);
  if (!file.startsWith(PUBLIC_DIR) || !fs.existsSync(file)) {
    response.writeHead(404);
    response.end('Not found');
    return;
  }
  response.writeHead(200, {
    'Content-Type': MIME[path.extname(file)] || 'application/octet-stream',
    // 控制台迭代频繁：禁止浏览器缓存静态文件，避免拿到旧 app.js 导致页面逻辑错乱。
    'Cache-Control': 'no-store, must-revalidate',
  });
  fs.createReadStream(file).pipe(response);
});

server.listen(PORT, () => {
  console.log(`BtB 控制台: http://localhost:${PORT}/`);
  startViewServers();
});

function shutdown() {
  if (bridge) bridge.kill();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
