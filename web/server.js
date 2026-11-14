'use strict';

// 单个网页取代原来的五个 tkinter 窗口：Pong 画面、UP / DOWN 指示灯、停止按钮、网络图。
// Node 负责静态页面与 SSE 推送，Python 子进程负责跑网络。

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

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
const latest = new Map(); // 类型 -> 最近一条消息，供新连接补发
let bridge = null;
let stopping = false;

function broadcast(message) {
  latest.set(message.type, message);
  const line = `data: ${JSON.stringify(message)}\n\n`;
  for (const client of clients) client.write(line);
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
      try {
        broadcast(JSON.parse(line));
      } catch (error) {
        console.error('无法解析子进程输出:', line.slice(0, 200));
      }
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
    for (const key of ['started', 'meta', 'state', 'graph', 'stopped', 'bridge_error', 'exit']) {
      if (latest.has(key)) response.write(`data: ${JSON.stringify(latest.get(key))}\n\n`);
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
  response.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(response);
});

server.listen(PORT, () => {
  console.log(`BtB 控制台: http://localhost:${PORT}/`);
});

function shutdown() {
  if (bridge) bridge.kill();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
