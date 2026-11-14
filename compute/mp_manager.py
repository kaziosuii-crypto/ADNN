import heapq
import json
import multiprocessing as mp
import os
import queue
import random
import sqlite3
import threading
import time

from mpmath import mp as precision

# 内存上限 100G，由父进程与所有 worker 共同分摊。
MEMORY_LIMIT = 100 * 1024 ** 3
DEFAULT_WORKERS = 28
# 收集结果、序列化、发网页固定用 2 个核。
CHANNELS = 2
# 每个节点用自己的时钟衰减：每秒一步，每步减 0.1；没有全局衰减节拍。
DECAY_INTERVAL = 1.0
DECAY_STEP = 0.1
# worker 每次只处理一小批到期节点就回去读命令，保证激活不会被到期队列堵住。
WORK_BATCH = 32
WORK_SPIN = 0.01
WORKER_JOIN_TIMEOUT = 60.0
# 落盘时每个事务的行数。
FLUSH_CHUNK = 5000
# 父进程等待事件消费者追平的时长。
PUMP_TIMEOUT = 0.1


class NodeView:
    def __init__(self, node_id, value, dps, window, forwards=None):
        self.id = node_id
        self.value = float(value)
        self.dps = dps
        self.timeWindow = window
        self.forwards = {int(key): value for key, value in (forwards or {}).items()}
        self.active = False
        self._lock = threading.RLock()

    def start(self, manager, value):
        manager.activate(self.id, value)


def _save_partition(db_path, nodes, cache_kib, save_lock):
    """worker 退出时直接把自己的分片写进数据库，省掉整份快照回传父进程的开销。

    28 个 worker 同时抢 SQLite 写锁会触发退避重试，单次提交被放大到几十毫秒；
    这里用一把跨进程锁排队，每次在无竞争状态下提交。
    """
    with save_lock:
        connection = sqlite3.connect(db_path, timeout=120.0)
        try:
            connection.execute("PRAGMA busy_timeout = 120000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA cache_size = -%d" % cache_kib)
            connection.executemany(
                "INSERT OR REPLACE INTO nodes (id, value, forwards) VALUES (?, ?, ?)",
                [(node.id, float(node.value), json.dumps(node.forwards)) for node in nodes.values()],
            )
            connection.commit()
        finally:
            connection.close()


def _worker(worker_id, count, commands, events, states, dps, window, stop, dopamine, db_path, cache_kib, save_lock):
    nodes = {node_id: NodeView(node_id, value, dps, window, forwards)
             for node_id, value, forwards in states}
    precision.dps = dps
    deadlines = []
    decays = []
    decay_at = {}
    started = time.monotonic()
    for node in nodes.values():
        if node.forwards:
            decay_at[node.id] = started + DECAY_INTERVAL
            heapq.heappush(decays, (decay_at[node.id], node.id))

    def get_node(node_id, value=None):
        if node_id not in nodes:
            nodes[node_id] = NodeView(node_id, node_id / 10 ** dps if value is None else value, dps, window)
        return nodes[node_id]

    def snapshot(node):
        return (node.id, float(node.value), node.active, dict(node.forwards))

    def emit(kind, data):
        events[int(data[0]) % CHANNELS].put((kind, worker_id, data))

    def schedule_decay(node):
        # 节点只在第一次产生连接时登记自己的衰减时钟，之后按自身节奏滚动。
        if node.id in decay_at or not node.forwards:
            return
        at = time.monotonic() + DECAY_INTERVAL
        decay_at[node.id] = at
        heapq.heappush(decays, (at, node.id))

    def handle(command):
        kind = command[0]
        if kind == "activate":
            node = get_node(command[1])
            node.value = 1 / (1 + precision.exp(-(node.value + command[2])))
            if not node.active:
                node.active = True
                heapq.heappush(deadlines, (time.monotonic() + window, node.id))
            emit("node", snapshot(node))
            schedule_decay(node)
        elif kind == "add":
            get_node(command[1], command[2])
        elif kind == "stop":
            return False
        return True

    try:
        running = True
        while running:
            handled = 0
            while handled < WORK_BATCH and not stop.is_set():
                now = time.monotonic()
                if deadlines and deadlines[0][0] <= now:
                    _, node_id = heapq.heappop(deadlines)
                    node = nodes[node_id]
                    target = int(precision.sin(precision.pi * node.value) * 10 ** dps)
                    level = dopamine.value
                    if level > 0:
                        target += int(random.uniform(-1, 1) * float(precision.sqrt(level)) * 10 ** dps)
                    node.forwards[target] = node.forwards.get(target, 0) + 1
                    node.active = False
                    state = snapshot(node)
                    emit("node", state)
                    for target_id, connection in state[3].items():
                        if stop.is_set():
                            break
                        if random.random() < min(1.0, connection):
                            commands[target_id % count].put(("activate", target_id, state[1]))
                    emit("callback", state)
                    schedule_decay(node)
                    handled += 1
                elif decays and decays[0][0] <= now:
                    at, node_id = heapq.heappop(decays)
                    handled += 1
                    if decay_at.get(node_id) != at:
                        # 节点被重新登记后，旧堆项直接跳过。
                        continue
                    node = nodes[node_id]
                    if node.forwards:
                        node.forwards = {target: weight - DECAY_STEP
                                         for target, weight in node.forwards.items() if weight > DECAY_STEP}
                        emit("node", snapshot(node))
                    if node.forwards:
                        decay_at[node_id] = at + DECAY_INTERVAL
                        heapq.heappush(decays, (decay_at[node_id], node_id))
                    else:
                        decay_at.pop(node_id, None)
                else:
                    break
            # 每批到期节点之后立刻清一小批命令，任何到期节点都不会独占 worker。
            for _ in range(WORK_BATCH):
                try:
                    command = commands[worker_id].get_nowait()
                except queue.Empty:
                    break
                if not handle(command):
                    running = False
                    break
            if not running:
                break
            if stop.is_set():
                # 停止时把队列里剩余命令全部处理完（含最后一批 add），收到 stop 标记即停。
                # 父进程在持锁状态下先排入所有命令再排 stop，顺序保证了这里不丢命令。
                drain_deadline = time.monotonic() + 5.0
                while running:
                    remaining = drain_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        command = commands[worker_id].get(timeout=min(remaining, 0.2))
                    except queue.Empty:
                        continue
                    if not handle(command):
                        running = False
                break
            if handled:
                continue
            # 空闲时阻塞等命令，超时取最近的节点时钟，避免空转。
            next_at = min(deadlines[0][0] if deadlines else float("inf"),
                          decays[0][0] if decays else float("inf"))
            timeout = max(0.0, min(WORK_SPIN, next_at - time.monotonic()))
            try:
                command = commands[worker_id].get(timeout=timeout)
            except queue.Empty:
                continue
            if not handle(command):
                break
    finally:
        try:
            _save_partition(db_path, nodes, cache_kib, save_lock)
        finally:
            # 停止时父进程可能已不再读取队列；取消后台发送线程的 join，避免进程无法退出。
            for channel in events:
                channel.cancel_join_thread()
            commands[worker_id].cancel_join_thread()


def _payload_part(nodes):
    rows, edges = [], []
    for node in nodes:
        with node._lock:
            rows.append([node.id, int(node.active), round(float(node.value), 6)])
            edges.extend([node.id, int(target), round(float(weight), 4)]
                         for target, weight in node.forwards.items())
    return rows, edges


def _split_build(nodes, builder):
    middle = len(nodes) // 2
    threads = [threading.Thread(target=builder, args=(0, nodes[:middle])),
               threading.Thread(target=builder, args=(1, nodes[middle:]))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


class MPNodeManager:
    def __init__(self, initid, initvalue, initdps, initTimeWindow, db_path="nodes.db", save_interval=2.0, worker_count=None):
        if save_interval is not None and save_interval <= 0:
            raise ValueError("save_interval must be positive or None")
        self.initid = int(initid)
        self.initvalue = float(initvalue)
        self.initdps = initdps
        self.initTimeWindow = initTimeWindow
        self._ctx = mp.get_context("spawn")
        self.worker_count = int(worker_count if worker_count is not None else DEFAULT_WORKERS)
        if self.worker_count < 1:
            raise ValueError("worker_count must be positive")
        self.db_path = os.path.abspath(db_path)
        # 父进程与每个 worker 平分 100G 预算。
        self.cache_kib = max(65536, MEMORY_LIMIT // ((self.worker_count + 2) * 1024))
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db_lock = threading.RLock()
        self._db.execute("PRAGMA busy_timeout = 30000")
        # WAL 让 28 个 worker 收尾落盘时不必轮流全量 fsync，结束速度取决于写入量而不是磁盘同步次数。
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.execute("PRAGMA cache_size = -%d" % self.cache_kib)
        self._db.execute("CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY, value REAL, forwards TEXT)")
        self._db.commit()
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._pump_lock = threading.RLock()
        self._dirty_lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._stop = threading.Event()
        self._consumer_stop = threading.Event()
        self._writer_stop = threading.Event()
        self._closed = False
        self.save_error = None
        self.cacheNodes = {}
        self.callbacks = {}
        self._dirty = set()
        self._callbacks_pending = []
        self._commands = [self._ctx.Queue() for _ in range(self.worker_count)]
        self._events = tuple(self._ctx.Queue() for _ in range(CHANNELS))
        self._idle = tuple(threading.Event() for _ in range(CHANNELS))
        for idle in self._idle:
            idle.set()
        # 两个消费者分别吃一条事件通道，收集结果本身就占满 2 个核。
        self._consumers = [threading.Thread(target=self._consume, args=(channel,), daemon=True)
                           for channel in range(CHANNELS)]
        for consumer in self._consumers:
            consumer.start()
        self._worker_stop = self._ctx.Event()
        self._dopamine = self._ctx.Value("d", 0.0)
        # 收尾落盘时 28 个 worker 共用这一把锁，避免在 SQLite 写锁上互相退避等待。
        self._save_lock = self._ctx.Lock()
        partitions = [[] for _ in range(self.worker_count)]
        for node_id, value, forwards in self._db.execute("SELECT id, value, forwards FROM nodes"):
            edges = json.loads(forwards or "{}")
            node = NodeView(node_id, value, initdps, initTimeWindow, edges)
            self.cacheNodes[node_id] = node
            partitions[node_id % self.worker_count].append((node_id, value, edges))
        self._processes = []
        for worker_id in range(self.worker_count):
            process = self._ctx.Process(target=_worker, args=(worker_id, self.worker_count, self._commands, self._events, partitions[worker_id], initdps, initTimeWindow, self._worker_stop, self._dopamine, self.db_path, self.cache_kib, self._save_lock))
            process.start()
            self._processes.append(process)
        self.addNode(initid, initvalue)
        self.save_interval = save_interval
        self._writer = None
        if save_interval is not None:
            self._writer = threading.Thread(target=self._write_loop, daemon=True)
            self._writer.start()

    def _write_loop(self):
        while not self._writer_stop.wait(self.save_interval):
            try:
                self.flush()
            except Exception as error:
                self.save_error = str(error)

    def _consume(self, channel):
        events = self._events[channel]
        idle = self._idle[channel]
        while not self._consumer_stop.is_set():
            try:
                event = events.get(timeout=0.05)
            except queue.Empty:
                idle.set()
                continue
            idle.clear()
            self._handle(event)
        idle.set()

    @property
    def dopamine(self):
        return self._dopamine.value

    @dopamine.setter
    def dopamine(self, value):
        self._dopamine.value = float(value)

    def add_dopamine(self, delta):
        with self._dopamine.get_lock():
            self._dopamine.value += delta

    @property
    def stopped(self):
        return self._stop.is_set()

    def activate(self, node_id, value):
        with self._lock:
            if not self.stopped:
                self._commands[int(node_id) % self.worker_count].put(("activate", int(node_id), float(value)))

    def addNode(self, node_id, value):
        node_id = int(node_id)
        with self._lock:
            node = self.cacheNodes.get(node_id)
            if node is None:
                node = NodeView(node_id, value, self.initdps, self.initTimeWindow)
                self.cacheNodes[node_id] = node
                with self._dirty_lock:
                    self._dirty.add(node_id)
                if not self.stopped:
                    self._commands[node_id % self.worker_count].put(("add", node_id, float(value)))
            return node

    def getNode(self, node_id):
        return self.addNode(node_id, node_id / 10 ** self.initdps)

    def getNodeFromCache(self, node_id):
        return self.cacheNodes.get(node_id)

    getNodeFromDatabase = getNodeFromCache

    def register_callback(self, node_id, fn):
        self.callbacks[node_id] = fn

    def _apply(self, state):
        node_id, value, active, forwards = state
        node = self.cacheNodes.get(node_id)
        if node is None:
            with self._lock:
                node = self.cacheNodes.get(node_id)
                if node is None:
                    node = NodeView(node_id, value, self.initdps, self.initTimeWindow)
                    self.cacheNodes[node_id] = node
        with node._lock:
            node.value, node.active, node.forwards = value, active, forwards
        with self._dirty_lock:
            self._dirty.add(node_id)

    def _handle(self, event):
        kind, worker_id, data = event
        if kind == "node":
            self._apply(data)
        elif kind == "callback":
            with self._callback_lock:
                self._callbacks_pending.append(data[0])

    def pump_events(self):
        """等两条事件通道追平，再把回调交给调用线程执行（回调仍在游戏线程里跑）。"""
        if self._closed:
            return 0
        deadline = time.monotonic() + PUMP_TIMEOUT
        for idle in self._idle:
            idle.wait(max(0.0, deadline - time.monotonic()))
        with self._callback_lock:
            pending, self._callbacks_pending = self._callbacks_pending, []
        count = 0
        for node_id in pending:
            callback = self.callbacks.get(node_id)
            if callback and not self.stopped:
                node = self.cacheNodes.get(node_id)
                if node is not None:
                    callback(node)
            count += 1
        return count

    def summary(self):
        self.pump_events()
        with self._lock:
            nodes = list(self.cacheNodes.values())
        return {"stopped": self.stopped, "saved": self._closed, "dopamine": self.dopamine,
                "save_error": self.save_error, "nodes": len(nodes),
                "active": sum(node.active for node in nodes), "edges": sum(len(node.forwards) for node in nodes)}

    def _graph_nodes(self):
        self.pump_events()
        with self._lock:
            return list(self.cacheNodes.values())

    def graph_payload(self):
        nodes = self._graph_nodes()
        parts = [None, None]

        def build(index, chunk):
            parts[index] = _payload_part(chunk)

        _split_build(nodes, build)
        rows, edges = [], []
        for part in parts:
            rows.extend(part[0])
            edges.extend(part[1])
        return {"nodes": rows, "edges": edges}

    def graph_json(self):
        """在 2 个核上并行完成节点/连线的序列化，返回可直接写入网页的 JSON 文本。"""
        nodes = self._graph_nodes()
        parts = [None, None]

        def build(index, chunk):
            rows, edges = _payload_part(chunk)
            parts[index] = (
                json.dumps(rows, separators=(",", ":"))[1:-1],
                json.dumps(edges, separators=(",", ":"))[1:-1],
            )

        _split_build(nodes, build)
        node_text = ",".join(part[0] for part in parts if part[0])
        edge_text = ",".join(part[1] for part in parts if part[1])
        return '{"nodes":[' + node_text + '],"edges":[' + edge_text + ']}'

    def flush(self):
        with self._dirty_lock:
            dirty, self._dirty = self._dirty, set()
        if not dirty:
            return
        with self._lock:
            nodes = [self.cacheNodes[node_id] for node_id in dirty if node_id in self.cacheNodes]
        rows = []
        for node in nodes:
            with node._lock:
                rows.append((node.id, float(node.value), json.dumps(node.forwards)))
        try:
            with self._db_lock:
                for start in range(0, len(rows), FLUSH_CHUNK):
                    self._db.executemany(
                        "INSERT OR REPLACE INTO nodes (id, value, forwards) VALUES (?, ?, ?)",
                        rows[start:start + FLUSH_CHUNK],
                    )
                self._db.commit()
        except Exception as error:
            with self._db_lock:
                self._db.rollback()
            with self._dirty_lock:
                self._dirty.update(dirty)
            self.save_error = str(error)
            raise
        self.save_error = None

    def request_stop(self):
        with self._lock:
            if self.stopped:
                return
            self._stop.set()
            self._worker_stop.set()
            self._writer_stop.set()
            for command in self._commands:
                command.put(("stop",))

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self.request_stop()
            if self._writer:
                self._writer.join()
            saved = True
            for process in self._processes:
                process.join(timeout=WORKER_JOIN_TIMEOUT)
                if process.is_alive():
                    process.terminate()
                    process.join()
                    saved = False
                elif process.exitcode:
                    saved = False
            self.pump_events()
            self._consumer_stop.set()
            for consumer in self._consumers:
                consumer.join()
            if saved:
                # worker 已经各自落盘，父进程缓存只用于展示，不能再写回覆盖。
                with self._dirty_lock:
                    self._dirty.clear()
            else:
                self.flush()
            with self._db_lock:
                self._db.close()
            with self._pump_lock:
                self._closed = True
                # worker 退出后没人再读这些队列；取消 join，避免解释器退出时卡住。
                for channel in self._events:
                    channel.cancel_join_thread()
                    channel.close()
                for command in self._commands:
                    command.cancel_join_thread()
                    command.close()


nodemgr = MPNodeManager
