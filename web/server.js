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
// 图帧缓存：一帧全量 + 其后的增量，新连接到达时按顺序补发即可得到完整图。
let fullGraphLine = null;
const patchLines = [];
// 增量帧缓存上限：超过后主动向 Python 要一帧全量，避免缓存无限增长。
const MAX_PATCHES = 64;
let graphRequested = false;
let bridge = null;
let stopping = false;

// 只提取类型，不解析整行 JSON；图帧可达数 MB，逐帧解析会白白吃掉 CPU。
const TYPE_RE = /^\{"type":"([a-z_]+)"/i;

function send(response, line) {
  response.write(`data: ${line}\n\n`);
}

// 向 Python 桥接进程请求一帧全量图；重复请求只保留一次，收到全量帧后自动复位。
function requestFullGraph() {
  if (!bridge || graphRequested) return;
  graphRequested = true;
  try {
    bridge.stdin.write(`${JSON.stringify({ cmd: 'graph' })}\n`);
    console.log(`增量帧已达上限 ${MAX_PATCHES}，请求全量图`);
  } catch (error) {
    graphRequested = false;
    console.error('请求全量图失败:', error.message);
  }
}

function broadcastLine(line) {
  const match = TYPE_RE.exec(line);
  const type = match ? match[1] : null;
  if (type === 'graph') {
    // 新的全量帧到达，之前积累的增量都可以丢弃。
    fullGraphLine = line;
    patchLines.length = 0;
    graphRequested = false;
  } else if (type === 'patch') {
    patchLines.push(line);
    if (patchLines.length >= MAX_PATCHES) requestFullGraph();
  } else if (type) {
    latest.set(type, line);
  }
  const frame = `data: ${line}\n\n`;
  for (const client of clients) client.write(frame);
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
    graphRequested = false;
    fullGraphLine = null;
    patchLines.length = 0;
    broadcast({ type: 'exit', code });
    console.log(`Python 桥接进程已退出（code=${code}）`);
  });
  latest.clear();
  fullGraphLine = null;
  patchLines.length = 0;
  graphRequested = false;
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
    // 先补发非图类的最近状态，再按“全量图 + 增量”顺序补发图结构。
    for (const key of ['started', 'meta', 'state', 'stopped', 'bridge_error', 'exit']) {
      if (latest.has(key)) send(response, latest.get(key));
    }
    if (fullGraphLine) send(response, fullGraphLine);
    for (const line of patchLines) send(response, line);
    clients.add(response);
    request.on('close', () => clients.delete(response));
    // 图缓存可能尚未就绪或已有少量偏差，直接向 Python 请求一帧最新全量。
    requestFullGraph();
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
  startViewServers();
});

function shutdown() {
  if (bridge) bridge.kill();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
