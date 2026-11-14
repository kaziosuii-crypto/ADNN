"""原生 C 引擎的 Python 封装（ctypes）。

计算、开火、传播、衰减、计时全部在 C 的分片线程里完成（不占用 GIL），
Python 只做三件事：启动时把 DB 里的节点/边灌进引擎、每拍投喂视觉位图、按采样节奏
取回“激活翻转 / 结构变化 / 统计”。

对外方法保持与 `MPNodeManager` 一致，PongRuntime 可以无缝切换。
"""

import ctypes
import json
import os
import sqlite3
import threading
import time

from compute.mp_manager import DEFAULT_WORKERS, FLUSH_CHUNK

# 采样缓冲上限：一次最多取回这么多条翻转/结构变化。
DRAIN_MAX = 262144
# 单个节点最多导出的出边条数（与旧实现的 degree 上限一致）。
EDGE_MAX = 4096


def _library_candidates():
    """按优先级给出原生库路径：环境变量 → c/ 目录 → 根目录。"""
    override = os.environ.get("BTB_NATIVE_LIB")
    if override:
        yield override
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("btb_core.dll", "libbtb_core.so", "btb_core.so", "btb_core.dylib"):
        yield os.path.join(here, "c", name)
        yield os.path.join(here, name)


def load_native_library():
    """加载原生库；找不到或加载失败时返回 None，调用方回退到多进程实现。"""
    for path in _library_candidates():
        if not os.path.exists(path):
            continue
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


def _bind(lib):
    """声明 ctypes 函数签名，避免指针被默认当成 int 截断。"""
    lib.btb_create.restype = ctypes.c_void_p
    lib.btb_create.argtypes = [ctypes.c_double, ctypes.c_int, ctypes.c_int]
    lib.btb_destroy.restype = None
    lib.btb_destroy.argtypes = [ctypes.c_void_p]
    lib.btb_decay_all.restype = None
    lib.btb_decay_all.argtypes = [ctypes.c_void_p]

    lib.btb_set_node.restype = ctypes.c_int
    lib.btb_set_node.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_double]
    lib.btb_add_edge.restype = ctypes.c_int
    lib.btb_add_edge.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_int64, ctypes.c_double]

    lib.btb_activate_many.restype = ctypes.c_int
    lib.btb_activate_many.argtypes = [ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_int64),
                                      ctypes.POINTER(ctypes.c_double),
                                      ctypes.c_int]
    lib.btb_activate_grid.restype = ctypes.c_int
    lib.btb_activate_grid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]

    lib.btb_set_dopamine.restype = None
    lib.btb_set_dopamine.argtypes = [ctypes.c_void_p, ctypes.c_double]
    lib.btb_get_dopamine.restype = ctypes.c_double
    lib.btb_get_dopamine.argtypes = [ctypes.c_void_p]
    lib.btb_add_dopamine.restype = None
    lib.btb_add_dopamine.argtypes = [ctypes.c_void_p, ctypes.c_double]

    lib.btb_stats.restype = None
    lib.btb_stats.argtypes = [ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_longlong),
                              ctypes.POINTER(ctypes.c_longlong),
                              ctypes.POINTER(ctypes.c_longlong)]

    lib.btb_drain_flips.restype = ctypes.c_int
    lib.btb_drain_flips.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_int64),
                                    ctypes.POINTER(ctypes.c_double),
                                    ctypes.POINTER(ctypes.c_ubyte),
                                    ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int)]
    lib.btb_drain_fires.restype = ctypes.c_int
    lib.btb_drain_fires.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_int64),
                                    ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int)]
    lib.btb_drain_dirty.restype = ctypes.c_int
    lib.btb_drain_dirty.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_int64),
                                    ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int)]

    lib.btb_node.restype = ctypes.c_int
    lib.btb_node.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                             ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_ubyte)]
    lib.btb_edges.restype = ctypes.c_int
    lib.btb_edges.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                              ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_double),
                              ctypes.c_int, ctypes.POINTER(ctypes.c_int)]

    lib.btb_export_all.restype = ctypes.c_int
    lib.btb_export_all.argtypes = [ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_int64),
                                   ctypes.POINTER(ctypes.c_double),
                                   ctypes.POINTER(ctypes.c_ubyte),
                                   ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_int64),
                                   ctypes.POINTER(ctypes.c_int64),
                                   ctypes.POINTER(ctypes.c_double),
                                   ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_int)]


class NativeNodeManager:
    """用 C 引擎跑神经元计算的管理器；接口与 MPNodeManager 对齐。"""

    def __init__(self, initid, initvalue, initdps, initTimeWindow, db_path="nodes.db",
                 save_interval=2.0, worker_count=None, visual_size=0):
        lib = load_native_library()
        if lib is None:
            raise RuntimeError("找不到原生库 btb_core（先编译 C 引擎）")
        _bind(lib)
        self._lib = lib
        self.initid = int(initid)
        self.initvalue = float(initvalue)
        self.initdps = initdps
        self.initTimeWindow = initTimeWindow
        self.visual_size = int(visual_size)
        self.worker_count = int(worker_count if worker_count is not None else DEFAULT_WORKERS)
        self.db_path = os.path.abspath(db_path)
        self._closed = False
        self.save_error = None
        self._stop = threading.Event()
        self._pending_persist = set()

        # 采样缓冲只分配一次，避免每帧新建大数组。
        self._ids = (ctypes.c_int64 * DRAIN_MAX)()
        self._values = (ctypes.c_double * DRAIN_MAX)()
        self._actives = (ctypes.c_ubyte * DRAIN_MAX)()
        self._out = ctypes.c_int(0)
        self._edge_targets = (ctypes.c_int64 * EDGE_MAX)()
        self._edge_weights = (ctypes.c_double * EDGE_MAX)()
        self._edge_out = ctypes.c_int(0)
        self._val = ctypes.c_double(0.0)
        self._act = ctypes.c_ubyte(0)

        self.callbacks = {}
        self._callbacks_pending = []
        self._slowest_ms = 0.0

        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db_lock = threading.RLock()
        self._db.execute("PRAGMA busy_timeout = 30000")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY, value REAL, forwards TEXT)")
        self._db.commit()

        # 先创建引擎句柄，再把 DB 里的节点/边灌进去（计时线程此期间只是空转）。
        self._handle = self._lib.btb_create(float(initTimeWindow), int(initdps), self.visual_size)
        if not self._handle:
            raise RuntimeError("btb_create 失败")
        loaded = self._db.execute("SELECT id, value, forwards FROM nodes").fetchall()
        for node_id, value, forwards in loaded:
            self._lib.btb_set_node(self._handle, int(node_id), float(value))
            for target, weight in json.loads(forwards or "{}").items():
                self._lib.btb_add_edge(self._handle, int(node_id), int(target), float(weight))
        self.addNode(self.initid, self.initvalue)

        self.save_interval = save_interval
        self._writer = None
        if save_interval:
            self._writer = threading.Thread(target=self._write_loop, daemon=True)
            self._writer.start()
        # 衰减由 Python 每秒触发一次，C 只执行“权重 -0.1、删边”这个神经元动作。
        self._decay_thread = threading.Thread(target=self._decay_loop, daemon=True)
        self._decay_thread.start()

    def _decay_loop(self):
        """每秒调用一次 btb_decay_all；引擎销毁后自然退出。"""
        while not self._stop.wait(1.0):
            handle = self._handle
            if handle:
                self._lib.btb_decay_all(handle)

    @property
    def dopamine(self):
        if not self._handle:
            return 0.0
        return float(self._lib.btb_get_dopamine(self._handle))

    @dopamine.setter
    def dopamine(self, value):
        if self._handle:
            self._lib.btb_set_dopamine(self._handle, float(value))

    def add_dopamine(self, delta):
        if self._handle:
            self._lib.btb_add_dopamine(self._handle, float(delta))

    @property
    def stopped(self):
        return self._stop.is_set() or self._closed

    def addNode(self, node_id, value):
        node_id = int(node_id)
        if self._handle:
            self._lib.btb_set_node(self._handle, node_id, float(value))
        self._pending_persist.add(node_id)
        return node_id

    def getNode(self, node_id):
        return self.addNode(node_id, int(node_id) / 10 ** self.initdps)

    def getNodeFromCache(self, node_id):
        """返回一个最小节点视图；游戏回调只需要 id，这里保持接口一致。"""
        if not self._handle:
            return None
        found = self._lib.btb_node(self._handle, int(node_id),
                                   ctypes.byref(self._val), ctypes.byref(self._act))
        if not found:
            return None
        return {"id": int(node_id), "value": float(self._val.value), "active": bool(self._act.value)}

    getNodeFromDatabase = getNodeFromCache

    def register_callback(self, node_id, fn):
        self.callbacks[int(node_id)] = fn

    def activate(self, node_id, value):
        if self.stopped or not self._handle:
            return
        ids = (ctypes.c_int64 * 1)(int(node_id))
        values = (ctypes.c_double * 1)(float(value))
        self._lib.btb_activate_many(self._handle, ids, values, 1)

    def activate_many(self, pairs):
        if self.stopped or not self._handle:
            return
        pairs = list(pairs)
        if not pairs:
            return
        ids = (ctypes.c_int64 * len(pairs))(*[int(item[0]) for item in pairs])
        values = (ctypes.c_double * len(pairs))(*[float(item[1]) for item in pairs])
        self._lib.btb_activate_many(self._handle, ids, values, len(pairs))

    def activate_visual(self, bits):
        """整帧视觉位图直接投喂引擎：各分片线程自己展开自己那 1/28 的神经元。"""
        if self.stopped or not self._handle:
            return
        count = len(bits)
        buffer = (ctypes.c_ubyte * count).from_buffer_copy(bits)
        self._lib.btb_activate_grid(self._handle, buffer, count)

    def pump_events(self, timeout=None):
        """把 C 侧的开火事件派发到 Python 回调（游戏操作依赖它）。"""
        if self._closed or not self._handle:
            return 0
        self._lib.btb_drain_fires(self._handle, self._ids, DRAIN_MAX, ctypes.byref(self._out))
        fired = self._out.value
        for index in range(fired):
            node_id = int(self._ids[index])
            callback = self.callbacks.get(node_id)
            if callback is not None and not self.stopped:
                node = self.getNodeFromCache(node_id)
                if node is not None:
                    callback(node)
        return fired

    def take_slowest_ms(self):
        """兼容接口：C 引擎不做单神经元计时，上层用 CPU 占用来调节帧率。"""
        return 0.0

    def summary(self):
        nodes = ctypes.c_longlong(0)
        active = ctypes.c_longlong(0)
        edges = ctypes.c_longlong(0)
        if self._handle:
            self._lib.btb_stats(self._handle, ctypes.byref(nodes), ctypes.byref(active), ctypes.byref(edges))
        return {"stopped": self.stopped, "saved": self._closed, "dopamine": self.dopamine,
                "save_error": self.save_error, "nodes": nodes.value,
                "active": active.value, "edges": edges.value}

    def _drain_flips(self):
        """取回激活翻转采样，并顺带记入待落盘集合。"""
        self._lib.btb_drain_flips(self._handle, self._ids, self._values,
                                  self._actives, DRAIN_MAX, ctypes.byref(self._out))
        taken = self._out.value
        rows = []
        for i in range(taken):
            node_id = int(self._ids[i])
            rows.append([node_id, int(self._actives[i]), round(float(self._values[i]), 6)])
            self._pending_persist.add(node_id)
        return rows

    def _drain_dirty(self):
        """取回出边集合发生变化的节点 ID。"""
        self._lib.btb_drain_dirty(self._handle, self._ids, DRAIN_MAX, ctypes.byref(self._out))
        taken = self._out.value
        ids = [int(self._ids[i]) for i in range(taken)]
        self._pending_persist.update(ids)
        return ids

    def _edges_for(self, node_id):
        self._lib.btb_edges(self._handle, int(node_id), self._edge_targets, self._edge_weights,
                            EDGE_MAX, ctypes.byref(self._edge_out))
        count = self._edge_out.value
        return [[int(self._edge_targets[i]), round(float(self._edge_weights[i]), 4)]
                for i in range(count)]

    def _read_node(self, node_id):
        found = self._lib.btb_node(self._handle, int(node_id),
                                   ctypes.byref(self._val), ctypes.byref(self._act))
        if not found:
            return None
        return [int(node_id), int(self._act.value), round(float(self._val.value), 6)]

    def used_ids(self):
        """启动时给分组分配用：返回所有已存在的节点 ID 与出边目标 ID。"""
        rows, edges = self._export_graph()
        used = {int(row[0]) for row in rows}
        used.update(int(target) for target in edges[1::3])
        return used

    def graph_patch_json(self):
        """增量图帧：激活翻转 + 结构变化的节点；边只对结构变化的节点附带。"""
        rows = self._drain_flips()
        dirty = self._drain_dirty()
        if not rows and not dirty:
            return None
        seen = {row[0] for row in rows}
        for node_id in dirty:
            if node_id in seen:
                continue
            row = self._read_node(node_id)
            if row is not None:
                rows.append(row)
                seen.add(node_id)
        edges = []
        edge_ids = []
        for node_id in dirty:
            edge_ids.append(int(node_id))
            for target, weight in self._edges_for(node_id):
                edges.extend([int(node_id), int(target), weight])
        return ('{"nodes":' + json.dumps(rows, separators=(",", ":"))
                + ',"edges":' + json.dumps(edges, separators=(",", ":"))
                + ',"edgeNodes":' + json.dumps(edge_ids, separators=(",", ":")) + '}')

    def graph_json(self, pump=True):
        """全量图帧：从 C 引擎导出全部节点与边。"""
        if pump:
            self.pump_events()
        rows, edges = self._export_graph()
        # 全量帧已经覆盖所有状态，清空增量缓冲避免紧接着重发。
        self._drain_flips()
        self._drain_dirty()
        return ('{"nodes":' + json.dumps(rows, separators=(",", ":"))
                + ',"edges":' + json.dumps(edges, separators=(",", ":")) + '}')

    def _export_graph(self):
        """一次 ctypes 调用导出整张图（rows + 扁平 edges），避免逐节点调用长时间占用 GIL。"""
        rows = []
        edges = []
        if not self._handle:
            return rows, edges
        total = ctypes.c_longlong(0)
        active = ctypes.c_longlong(0)
        edge_total = ctypes.c_longlong(0)
        self._lib.btb_stats(self._handle, ctypes.byref(total), ctypes.byref(active),
                            ctypes.byref(edge_total))
        node_cap = max(1, int(total.value))
        edge_cap = max(1024, int(edge_total.value * 1.15) + 1024)
        id_buffer = (ctypes.c_int64 * node_cap)()
        value_buffer = (ctypes.c_double * node_cap)()
        active_buffer = (ctypes.c_ubyte * node_cap)()
        edge_src = (ctypes.c_int64 * edge_cap)()
        edge_dst = (ctypes.c_int64 * edge_cap)()
        edge_weight = (ctypes.c_double * edge_cap)()
        out_edges = ctypes.c_int(0)
        written = self._lib.btb_export_all(self._handle, id_buffer, value_buffer, active_buffer,
                                           node_cap, edge_src, edge_dst, edge_weight,
                                           edge_cap, ctypes.byref(out_edges))
        for i in range(written):
            rows.append([int(id_buffer[i]), int(active_buffer[i]),
                         round(float(value_buffer[i]), 6)])
        for i in range(out_edges.value):
            edges.extend([int(edge_src[i]), int(edge_dst[i]), round(float(edge_weight[i]), 4)])
        return rows, edges

    def _persist(self, node_ids):
        if not node_ids:
            return
        rows = []
        for node_id in node_ids:
            row = self._read_node(node_id)
            if row is None:
                continue
            forwards = {str(target): weight for target, weight in self._edges_for(node_id)}
            rows.append((row[0], float(row[2]), json.dumps(forwards)))
        if not rows:
            return
        with self._db_lock:
            for start in range(0, len(rows), FLUSH_CHUNK):
                self._db.executemany(
                    "INSERT OR REPLACE INTO nodes (id, value, forwards) VALUES (?, ?, ?)",
                    rows[start:start + FLUSH_CHUNK],
                )
            self._db.commit()

    def flush(self):
        with self._db_lock:
            pending, self._pending_persist = self._pending_persist, set()
        try:
            self._persist(pending)
        except Exception as error:
            self._db.rollback()
            self._pending_persist.update(pending)
            self.save_error = str(error)
            raise
        self.save_error = None

    def _write_loop(self):
        while not self._stop.wait(self.save_interval):
            try:
                self.flush()
            except Exception as error:
                self.save_error = str(error)

    def save_all_and_stop(self):
        """递归保存全部节点后关闭引擎。"""
        if self._closed:
            return
        started = time.monotonic()
        self._stop.set()
        if self._writer is not None:
            self._writer.join()
        if self._decay_thread is not None:
            self._decay_thread.join()
        try:
            rows, edges = self._export_graph()
            by_src = {}
            for index in range(0, len(edges), 3):
                by_src.setdefault(edges[index], []).append((edges[index + 1], edges[index + 2]))
            payload = []
            for node_id, _active, value in rows:
                forwards = {str(target): weight for target, weight in by_src.get(node_id, ())}
                payload.append((node_id, float(value), json.dumps(forwards)))
            with self._db_lock:
                for start in range(0, len(payload), FLUSH_CHUNK):
                    self._db.executemany(
                        "INSERT OR REPLACE INTO nodes (id, value, forwards) VALUES (?, ?, ?)",
                        payload[start:start + FLUSH_CHUNK],
                    )
                self._db.commit()
        except Exception as error:
            self.save_error = str(error)
        finally:
            if self._handle:
                self._lib.btb_destroy(self._handle)
                self._handle = None
            with self._db_lock:
                self._db.close()
            self._closed = True
        if os.environ.get("BTB_DEBUG") == "1":
            print(f"[native_manager] 结束并落盘耗时 {(time.monotonic() - started) * 1000:.1f} ms",
                  flush=True)

    def close(self):
        self.save_all_and_stop()
