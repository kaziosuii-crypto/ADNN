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


def _worker(worker_id, count, commands, events, states, dps, window, stop, dopamine):
    nodes = {node_id: NodeView(node_id, value, dps, window, forwards)
             for node_id, value, forwards in states}
    precision.dps = dps
    deadlines = []
    next_decay = time.monotonic() + 1

    def get_node(node_id, value=None):
        if node_id not in nodes:
            nodes[node_id] = NodeView(node_id, node_id / 10 ** dps if value is None else value, dps, window)
        return nodes[node_id]

    def snapshot(node):
        return (node.id, float(node.value), node.active, dict(node.forwards))

    try:
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_decay:
                for node in nodes.values():
                    if node.forwards:
                        node.forwards = {target: weight - 0.1 for target, weight in node.forwards.items() if weight > 0.1}
                        events.put(("node", worker_id, snapshot(node)))
                next_decay = now + 1
            while deadlines and deadlines[0][0] <= now:
                _, node_id = heapq.heappop(deadlines)
                node = nodes[node_id]
                target = int(precision.sin(precision.pi * node.value) * 10 ** dps)
                level = dopamine.value
                if level > 0:
                    target += int(random.uniform(-1, 1) * float(precision.sqrt(level)) * 10 ** dps)
                node.forwards[target] = node.forwards.get(target, 0) + 1
                node.active = False
                state = snapshot(node)
                events.put(("node", worker_id, state))
                for target, connection in state[3].items():
                    if stop.is_set():
                        break
                    if random.random() < min(1.0, connection):
                        commands[target % count].put(("activate", target, state[1]))
                events.put(("callback", worker_id, state))
            try:
                command = commands[worker_id].get(timeout=min(0.02, max(0, next_decay - time.monotonic())))
            except queue.Empty:
                continue
            if command[0] == "activate":
                node = get_node(command[1])
                node.value = 1 / (1 + precision.exp(-(node.value + command[2])))
                if not node.active:
                    node.active = True
                    heapq.heappush(deadlines, (time.monotonic() + window, node.id))
                events.put(("node", worker_id, snapshot(node)))
            elif command[0] == "add":
                get_node(command[1], command[2])
            elif command[0] == "stop":
                break
        for node in nodes.values():
            node.active = False
        events.put(("stopped", worker_id, [snapshot(node) for node in nodes.values()]))
    finally:
        # 停止时父进程可能已不再读取队列；取消后台发送线程的 join，避免进程无法退出。
        events.cancel_join_thread()
        commands[worker_id].cancel_join_thread()


class MPNodeManager:
    def __init__(self, initid, initvalue, initdps, initTimeWindow, db_path="nodes.db", save_interval=2.0, worker_count=None):
        if save_interval is not None and save_interval <= 0:
            raise ValueError("save_interval must be positive or None")
        self.initid = int(initid)
        self.initvalue = float(initvalue)
        self.initdps = initdps
        self.initTimeWindow = initTimeWindow
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db_lock = threading.RLock()
        self._db.execute("CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY, value REAL, forwards TEXT)")
        self._db.commit()
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._pump_lock = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        self.save_error = None
        self.cacheNodes = {}
        self.callbacks = {}
        self._dirty = set()
        self._ctx = mp.get_context("spawn")
        self.worker_count = int(worker_count if worker_count is not None else min(4, mp.cpu_count()))
        if self.worker_count < 1:
            raise ValueError("worker_count must be positive")
        self._commands = [self._ctx.Queue() for _ in range(self.worker_count)]
        self._events = self._ctx.Queue()
        self._worker_stop = self._ctx.Event()
        self._dopamine = self._ctx.Value("d", 0.0)
        partitions = [[] for _ in range(self.worker_count)]
        for node_id, value, forwards in self._db.execute("SELECT id, value, forwards FROM nodes"):
            edges = json.loads(forwards or "{}")
            node = NodeView(node_id, value, initdps, initTimeWindow, edges)
            self.cacheNodes[node_id] = node
            partitions[node_id % self.worker_count].append((node_id, value, edges))
        self._processes = []
        for worker_id in range(self.worker_count):
            process = self._ctx.Process(target=_worker, args=(worker_id, self.worker_count, self._commands, self._events, partitions[worker_id], initdps, initTimeWindow, self._worker_stop, self._dopamine))
            process.start()
            self._processes.append(process)
        self.addNode(initid, initvalue)
        self.save_interval = save_interval
        self._writer_stop = threading.Event()
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
        with self._lock:
            node = self.cacheNodes.get(node_id)
            if node is None:
                node = NodeView(node_id, value, self.initdps, self.initTimeWindow)
                self.cacheNodes[node_id] = node
            with node._lock:
                node.value, node.active, node.forwards = value, active, forwards
            self._dirty.add(node_id)

    def _handle(self, event):
        kind, worker_id, data = event
        if kind == "stopped":
            for state in data:
                self._apply(state)
        elif kind == "node":
            self._apply(data)
        elif kind == "callback":
            callback = self.callbacks.get(data[0])
            if callback and not self.stopped:
                callback(self.cacheNodes[data[0]])

    def pump_events(self, limit=None):
        count = 0
        with self._pump_lock:
            if self._closed:
                return 0
            while limit is None or count < limit:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                self._handle(event)
                count += 1
        return count

    def summary(self):
        self.pump_events()
        with self._lock:
            nodes = list(self.cacheNodes.values())
        return {"stopped": self.stopped, "saved": self._closed, "dopamine": self.dopamine,
                "save_error": self.save_error, "nodes": len(nodes),
                "active": sum(node.active for node in nodes), "edges": sum(len(node.forwards) for node in nodes)}

    def graph_payload(self):
        self.pump_events()
        with self._lock:
            nodes = list(self.cacheNodes.values())
        rows, edges = [], []
        for node in nodes:
            with node._lock:
                rows.append([node.id, int(node.active), round(float(node.value), 6)])
                edges.extend([node.id, int(target), round(float(weight), 4)] for target, weight in node.forwards.items())
        return {"nodes": rows, "edges": edges}

    def flush(self):
        self.pump_events()
        with self._lock:
            dirty = set(self._dirty)
            rows = [(node_id, float(self.cacheNodes[node_id].value), json.dumps(self.cacheNodes[node_id].forwards)) for node_id in dirty]
            with self._db_lock:
                try:
                    self._db.executemany("INSERT OR REPLACE INTO nodes (id, value, forwards) VALUES (?, ?, ?)", rows)
                    self._db.commit()
                except Exception as error:
                    self._db.rollback()
                    self.save_error = str(error)
                    raise
            self._dirty.difference_update(dirty)
            self.save_error = None

    def request_stop(self):
        with self._lock:
            if self.stopped:
                return
            self._stop.set()
            self._worker_stop.set()
            for command in self._commands:
                command.put(("stop",))

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self.request_stop()
            for process in self._processes:
                process.join(timeout=15)
                if process.is_alive():
                    process.terminate()
                    process.join()
            self.pump_events()
            if self._writer:
                self._writer_stop.set()
                self._writer.join()
            self.flush()
            with self._db_lock:
                self._db.close()
            with self._pump_lock:
                self._closed = True
                # worker 退出后没人再读这些队列；取消 join，避免解释器退出时卡住。
                for command in self._commands:
                    command.cancel_join_thread()
                    command.close()
                self._events.cancel_join_thread()
                self._events.close()


nodemgr = MPNodeManager
