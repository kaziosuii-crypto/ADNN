import json
import sqlite3
import threading
import time

from .node import Node


class nodemgr:
    def __init__(self, initid, initvalue, initdps, initTimeWindow, db_path="nodes.db", save_interval=2.0):
        self.cacheNodes = {}
        self._lock = threading.RLock()
        self.initid = initid
        self.initvalue = initvalue
        self.initdps = initdps
        self.initTimeWindow = initTimeWindow
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db_lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY, value REAL, forwards TEXT)"
        )
        self._db.commit()
        self._dirty = set()
        self._dirty_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._writer_stop = threading.Event()
        self._stop = threading.Event()
        self._workers = threading.Condition()
        self._worker_count = 0
        self._close_lock = threading.Lock()
        self._closed = False
        self.save_interval = save_interval
        self.save_error = None
        self.dopamine = 0.0
        self.callbacks = {}  # node_id -> callable(node)
        # Load once before starting the network. Runtime lookups never query SQLite.
        for id, value, forwards in self._db.execute("SELECT id, value, forwards FROM nodes"):
            node = Node(value, id, self.initdps, self.initTimeWindow)
            node.forwards = {int(k): v for k, v in json.loads(forwards).items()}
            self.cacheNodes[id] = node
        self.addNode(self.initid, self.initvalue)
        self._decay_thread = threading.Thread(target=self._decay_loop, daemon=True)
        self._decay_thread.start()
        self._writer = None
        if save_interval is not None:
            self._writer = threading.Thread(target=self._write_loop, daemon=True)
            self._writer.start()

    def add_dopamine(self, delta):
        self.dopamine += delta

    def register_callback(self, node_id, fn):
        self.callbacks[node_id] = fn

    @property
    def stopped(self):
        return self._stop.is_set()

    def _enter_worker(self):
        with self._workers:
            if self.stopped:
                return False
            self._worker_count += 1
            return True

    def _leave_worker(self):
        with self._workers:
            self._worker_count -= 1
            self._workers.notify_all()

    def request_stop(self):
        with self._workers:
            self._stop.set()

    def _decay_loop(self):
        while not self.stopped:
            time.sleep(1.0)
            if self.stopped:
                break
            with self._lock:
                nodes = list(self.cacheNodes.values())
            for node in nodes:
                with node._lock:
                    if not node.forwards:
                        continue
                    node.forwards = {tid: w - 0.1 for tid, w in node.forwards.items() if w > 0.1}
                self.saveNodeToDatabase(node.id)

    def getNode(self, id):
        with self._lock:
            node = self.cacheNodes.get(id)
            if node is None:
                node = Node(id / 10 ** self.initdps, id, self.initdps, self.initTimeWindow)
                self.cacheNodes[id] = node
            return node

    def addNode(self, id, value):
        with self._lock:
            if id not in self.cacheNodes:
                self.cacheNodes[id] = Node(value, id, self.initdps, self.initTimeWindow)
            return self.cacheNodes[id]

    def getNodeFromCache(self, id):
        with self._lock:
            return self.cacheNodes.get(id)

    def getNodeFromDatabase(self, id):
        """Compatibility lookup: persisted nodes are already resident in memory."""
        return self.getNodeFromCache(id)

    def saveNodeToDatabase(self, id):
        """Mark a node for the next background batch; flush() waits for disk."""
        with self._dirty_lock:
            self._dirty.add(id)

    def flush(self):
        with self._flush_lock:
            with self._dirty_lock:
                dirty, self._dirty = self._dirty, set()
            with self._lock:
                nodes = [self.cacheNodes[id] for id in dirty if id in self.cacheNodes]
            rows = []
            for node in nodes:
                with node._lock:
                    value, forwards = float(node.value), dict(node.forwards)
                rows.append((node.id, value, json.dumps(forwards)))
            if rows:
                try:
                    with self._db_lock:
                        self._db.executemany(
                            "INSERT OR REPLACE INTO nodes (id, value, forwards) VALUES (?, ?, ?)",
                            rows,
                        )
                        self._db.commit()
                    self.save_error = None
                except Exception:
                    with self._db_lock:
                        self._db.rollback()
                    with self._dirty_lock:
                        self._dirty.update(dirty)
                    raise

    def _write_loop(self):
        while not self._writer_stop.wait(self.save_interval):
            try:
                self.flush()
            except Exception as error:
                self.save_error = str(error)

    def snapshot(self):
        """Observational snapshot, without synchronizing activation windows."""
        with self._lock:
            nodes = list(self.cacheNodes.values())
        return {
            "stopped": self.stopped,
            "saved": self._closed,
            "dopamine": self.dopamine,
            "save_error": self.save_error,
            "nodes": [(node.id, node.active, float(node.value), len(node.forwards)) for node in nodes],
        }

    def summary(self):
        """只统计数量，不构造节点列表，供高频状态推送使用。"""
        with self._lock:
            nodes = list(self.cacheNodes.values())
        return {
            "stopped": self.stopped,
            "saved": self._closed,
            "dopamine": self.dopamine,
            "save_error": self.save_error,
            "nodes": len(nodes),
            "active": sum(1 for node in nodes if node.active),
            "edges": sum(len(node.forwards) for node in nodes),
        }

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self.request_stop()
            with self._workers:
                self._workers.wait_for(lambda: self._worker_count == 0)
            self._decay_thread.join()
            self._writer_stop.set()
            if self._writer:
                self._writer.join()
            with self._lock:
                ids = list(self.cacheNodes)
            with self._dirty_lock:
                self._dirty.update(ids)
            self.flush()
            with self._db_lock:
                self._db.close()
            self._closed = True
