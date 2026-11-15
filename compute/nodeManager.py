"""神经元群体层：创建神经元、收发信号、调度执行与图结构持久化。

分工：
- `node.py`：一个神经元的 SMCGP14 参考实现（纯 Python）；
- `nodeManager.py`：神经元群体，负责外层寻址、消息汇总、调度与落盘；
  优先使用 C 版实现 `c/smcgp_node.c`（同样的 SMCGP14 边界，通过 ctypes 互操作），
  找不到库时自动回退到纯 Python 的 `Node`。

本层不实现任何激活函数、dopamine 或强化学习逻辑，只做路由与调度。
"""

from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import sys
import threading
import time
from typing import Dict, Iterable, List, Optional

from .node import Gene, Node, Op

# C 库文件名（按平台优先顺序尝试）。
_LIBRARY_NAMES = ("libsmcgp_node.so", "smcgp_node.dll", "smcgp_node.so", "libsmcgp_node.dylib")


def _bind_library(lib) -> None:
    """声明 C 接口签名；不声明的话 ctypes 会把 64 位指针截断成 int。"""
    lib.smcgp_create.restype = ctypes.c_void_p
    lib.smcgp_create.argtypes = [ctypes.c_int]
    lib.smcgp_destroy.restype = None
    lib.smcgp_destroy.argtypes = [ctypes.c_void_p]
    lib.smcgp_set_base_seed.restype = None
    lib.smcgp_set_base_seed.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.smcgp_add_node.restype = ctypes.c_int
    lib.smcgp_add_node.argtypes = [ctypes.c_void_p, ctypes.c_int64]
    lib.smcgp_set_genome.restype = ctypes.c_int
    lib.smcgp_set_genome.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                                     ctypes.POINTER(ctypes.c_int32),
                                     ctypes.POINTER(ctypes.c_double),
                                     ctypes.c_int, ctypes.c_uint64]
    lib.smcgp_get_genome.restype = ctypes.c_int
    lib.smcgp_get_genome.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                                     ctypes.POINTER(ctypes.c_int32),
                                     ctypes.POINTER(ctypes.c_double), ctypes.c_int]
    lib.smcgp_activate.restype = ctypes.c_int
    lib.smcgp_activate.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_double]
    lib.smcgp_activate_many.restype = ctypes.c_int
    lib.smcgp_activate_many.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_int64),
                                        ctypes.POINTER(ctypes.c_double), ctypes.c_int]
    lib.smcgp_activate_visual.restype = ctypes.c_int
    lib.smcgp_activate_visual.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
    lib.smcgp_run_round.restype = ctypes.c_int
    lib.smcgp_run_round.argtypes = [ctypes.c_void_p]
    lib.smcgp_take_fired.restype = ctypes.c_int
    lib.smcgp_take_fired.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64), ctypes.c_int]
    lib.smcgp_take_inputs.restype = ctypes.c_int
    lib.smcgp_take_inputs.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                                      ctypes.POINTER(ctypes.c_double), ctypes.c_int]
    lib.smcgp_list_nodes.restype = ctypes.c_int
    lib.smcgp_list_nodes.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64), ctypes.c_int]
    lib.smcgp_take_modified.restype = ctypes.c_int
    lib.smcgp_take_modified.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64),
                                        ctypes.c_int]
    lib.smcgp_pending_nodes.restype = ctypes.c_int
    lib.smcgp_pending_nodes.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64), ctypes.c_int]
    lib.smcgp_has_node.restype = ctypes.c_int
    lib.smcgp_has_node.argtypes = [ctypes.c_void_p, ctypes.c_int64]
    lib.smcgp_stats.restype = None
    lib.smcgp_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong),
                                ctypes.POINTER(ctypes.c_longlong)]


def load_smcgp_library():
    """从项目 c/ 目录加载 C 版 SMCGP14 库；不存在或签名不符时返回 None。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in _LIBRARY_NAMES:
        path = os.path.join(root, "c", name)
        if not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
            _bind_library(lib)
        except (OSError, AttributeError):
            continue
        if os.environ.get("BTB_DEBUG") == "1":
            # 诊断信息必须走 stderr：bridge 的 stdout 是纯 JSON 数据流。
            print(f"[nodeManager] 使用 C 版 SMCGP14: {path}",
                  file=sys.stderr, flush=True)
        return lib
    if os.environ.get("BTB_DEBUG") == "1":
        print("[nodeManager] 未找到 C 版 SMCGP14，回退纯 Python 实现",
              file=sys.stderr, flush=True)
    return None


class nodemgr:
    """神经元群体：ID -> SMCGP14 图；原生模式由 C 引擎持有图与调度。"""

    # 单次取回已触发节点 ID 的缓冲上限：dps=4 时节点上限 10000，足够。
    FIRED_CAP = 65536

    def __init__(self, dps: int = 4, seed=None, db_path: str = "nodes.db",
                 save_interval: Optional[float] = 2.0):
        """创建群体；图结构存放在独立表 smcgp_nodes，不碰旧版 nodes 表。"""
        self.dps = int(dps)
        self.seed = seed
        self.save_interval = save_interval
        self.save_error: Optional[str] = None
        self.callbacks: Dict[int, object] = {}
        self._lock = threading.RLock()
        self._dirty = set()
        self._dirty_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._lib = load_smcgp_library()
        self._handle = None
        if self._lib is not None:
            self._handle = self._lib.smcgp_create(self.dps)
            if self._handle:
                base = 0x2545F4914F6CDD1D
                if seed is not None:
                    base = (int(seed) * 2654435761 + 1) & 0xFFFFFFFFFFFFFFFF
                self._lib.smcgp_set_base_seed(self._handle, ctypes.c_uint64(base))
        # 纯 Python 回退用：ID -> Node；原生模式只记录 ID 以便观测/落盘。
        self.cacheNodes: Dict[int, Node] = {}
        self._known_ids = set()
        self._pending: Dict[int, List[float]] = {}
        self._fired_buffer = (ctypes.c_int64 * self.FIRED_CAP)()
        self._id_buffer = (ctypes.c_int64 * self.FIRED_CAP)()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db_lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS smcgp_nodes "
            "(id INTEGER PRIMARY KEY, graph TEXT NOT NULL)"
        )
        self._db.commit()
        self._load_from_db()
        self._writer = None
        if save_interval is not None:
            self._writer = threading.Thread(target=self._write_loop, name="smcgp-writer", daemon=True)
            self._writer.start()

    # ---------- 引擎状态 ----------

    @property
    def native(self) -> bool:
        """是否正在使用 C 引擎。"""
        return self._handle is not None

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ---------- 外层寻址 ----------

    def route_id(self, destination: float) -> int:
        """外层寻址规则：目标编码向零截断取整、负值取绝对值，再对 10^dps 取模。"""
        node_id = int(destination)
        if node_id < 0:
            node_id = -node_id
        return node_id % (10 ** self.dps)

    # ---------- 神经元创建与查询 ----------

    def addNode(self, node_id: int, graph: Optional[Iterable[Gene]] = None,
                seed=None) -> Optional[Node]:
        """按 ID 创建神经元；已存在时直接返回，不覆盖已有图。"""
        node_id = int(node_id)
        if self._handle:
            if graph is None:
                created = self._lib.smcgp_add_node(self._handle, node_id)
            else:
                created = self._set_genome_native(node_id, list(graph), seed)
            if created:
                self.saveNodeToDatabase(node_id)
            with self._lock:
                self._known_ids.add(node_id)
            return None
        with self._lock:
            node = self.cacheNodes.get(node_id)
            if node is None:
                node = Node(node_id, graph, seed=self.seed if seed is None else seed)
                self.cacheNodes[node_id] = node
            return node

    def getNode(self, node_id: int):
        """取神经元；不存在时按默认种子图创建（原生模式返回 None，图在 C 侧）。"""
        return self.addNode(node_id)

    def getNodeFromCache(self, node_id: int):
        """只查缓存，不创建新神经元（原生模式返回最小视图）。"""
        node_id = int(node_id)
        if self._handle:
            with self._lock:
                return {"id": node_id} if node_id in self._known_ids else None
        with self._lock:
            return self.cacheNodes.get(node_id)

    def used_ids(self) -> set:
        """启动时给分组分配用：返回已存在的全部神经元 ID。"""
        if self._handle:
            return set(self._native_ids())
        with self._lock:
            return set(self.cacheNodes)

    def register_callback(self, node_id: int, fn) -> None:
        """登记某个神经元“本轮产生过输出”时触发的回调。"""
        self.callbacks[int(node_id)] = fn

    # ---------- 收发信号 ----------

    def activate(self, node_id: int, value: float) -> int:
        """给一个神经元投递一个输入值（下一轮执行）。"""
        node_id = int(node_id)
        if self._handle:
            with self._lock:
                self._known_ids.add(node_id)
            return self._lib.smcgp_activate(self._handle, node_id, float(value))
        with self._lock:
            node = self.getNode(node_id)
            self._pending.setdefault(node_id, []).append(float(value))
            return 1 if node is not None else 0

    def activate_many(self, pairs: Iterable[tuple]) -> int:
        """批量投递输入；pairs 为 (node_id, value)。"""
        pairs = [(int(item[0]), float(item[1])) for item in pairs]
        if not pairs:
            return 0
        if self._handle:
            ids = (ctypes.c_int64 * len(pairs))(*[item[0] for item in pairs])
            values = (ctypes.c_double * len(pairs))(*[item[1] for item in pairs])
            with self._lock:
                self._known_ids.update(item[0] for item in pairs)
            return self._lib.smcgp_activate_many(self._handle, ids, values, len(pairs))
        accepted = 0
        for node_id, value in pairs:
            accepted += self.activate(node_id, value)
        return accepted

    def activate_visual(self, bits) -> int:
        """整帧视觉位图：只把 bit=1 的像素（id = 下标）作为输入 1.0 投递。"""
        if self._handle:
            buffer = (ctypes.c_ubyte * len(bits)).from_buffer_copy(bytes(bits))
            with self._lock:
                self._known_ids.update(index for index, bit in enumerate(bits) if bit)
            return self._lib.smcgp_activate_visual(self._handle, buffer, len(bits))
        accepted = 0
        for index, bit in enumerate(bits):
            if bit:
                accepted += self.activate(index, 1.0)
        return accepted

    def pump_events(self) -> int:
        """跑一轮：执行所有待处理神经元、把输出汇总成下一轮输入，并派发回调。

        返回本轮真正产生过输出的神经元数量。
        """
        if self._handle:
            executed = self._lib.smcgp_run_round(self._handle)
            fired = self._lib.smcgp_take_fired(self._handle, self._fired_buffer,
                                               self.FIRED_CAP)
            fired_ids = [int(self._fired_buffer[i]) for i in range(min(fired, self.FIRED_CAP))]
            with self._lock:
                self._known_ids.update(fired_ids)
            for node_id in fired_ids:
                callback = self.callbacks.get(node_id)
                if callback is not None and not self.stopped:
                    callback({"id": node_id})
            if executed < 0:
                self.save_error = "C 引擎执行失败"
            return len(fired_ids)
        return self._pump_python()

    def _pump_python(self) -> int:
        """纯 Python 回退：执行待处理输入并把输出路由成下一轮输入。"""
        with self._lock:
            pending, self._pending = self._pending, {}
        if not pending:
            return 0
        deliveries: Dict[int, List[float]] = {}
        fired_ids = []
        for node_id in sorted(pending):
            node = self.getNodeFromCache(node_id) or self.getNode(node_id)
            if node is None:
                continue
            emissions = node.run(pending[node_id])
            if emissions:
                fired_ids.append(node_id)
            for emission in emissions:
                target = self.route_id(emission.destination)
                deliveries.setdefault(target, []).append(float(emission.value))
        with self._lock:
            for target, values in deliveries.items():
                self.getNode(target)
                self._pending.setdefault(target, []).extend(values)
        for node_id in fired_ids:
            callback = self.callbacks.get(node_id)
            if callback is not None and not self.stopped:
                callback({"id": node_id})
        return len(fired_ids)

    def run_round(self, inputs_by_id: Dict[int, List[float]]) -> Dict[int, List[float]]:
        """显式跑一轮（测试/嵌入式用法）：投递输入、执行、返回下一轮待处理输入。"""
        for node_id, values in inputs_by_id.items():
            for value in values:
                self.activate(int(node_id), float(value))
        if self._handle:
            self._lib.smcgp_run_round(self._handle)
            self._lib.smcgp_take_fired(self._handle, self._fired_buffer, self.FIRED_CAP)
            deliveries: Dict[int, List[float]] = {}
            pending = self._native_pending_ids()
            buffer = (ctypes.c_double * 64)()
            for node_id in pending:
                count = self._lib.smcgp_take_inputs(self._handle, node_id, buffer, 64)
                deliveries[node_id] = [float(buffer[i]) for i in range(min(count, 64))]
            return deliveries
        pending_inputs = dict(self._pending)
        self._pending = {}
        executed = []
        for node_id in sorted(pending_inputs):
            node = self.getNode(node_id)
            if node is None:
                continue
            executed.append((node_id, node.run(pending_inputs[node_id])))
        deliveries = {}
        for _, emissions in executed:
            for emission in emissions:
                target = self.route_id(emission.destination)
                deliveries.setdefault(target, []).append(float(emission.value))
        with self._lock:
            for target, values in deliveries.items():
                self.getNode(target)
                self._pending.setdefault(target, []).extend(values)
        return deliveries

    # ---------- 统计 ----------

    def summary(self) -> dict:
        """只统计数量，不构造节点列表。"""
        if self._handle:
            nodes = ctypes.c_longlong(0)
            genes = ctypes.c_longlong(0)
            self._lib.smcgp_stats(self._handle, ctypes.byref(nodes), ctypes.byref(genes))
            return {"stopped": self.stopped, "saved": self._closed,
                    "save_error": self.save_error,
                    "nodes": int(nodes.value), "genes": int(genes.value)}
        with self._lock:
            nodes = list(self.cacheNodes.values())
        return {"stopped": self.stopped, "saved": self._closed,
                "save_error": self.save_error,
                "nodes": len(nodes), "genes": sum(len(node.graph) for node in nodes)}

    # ---------- 原生引擎的图读写 ----------

    def _set_genome_native(self, node_id: int, graph: List[Gene], seed=None) -> int:
        """把图结构写入 C 引擎。"""
        count = len(graph)
        layout = (ctypes.c_int32 * (count * 3))()
        params = (ctypes.c_double * (count * 3))()
        for index, gene in enumerate(graph):
            layout[index * 3] = int(gene.op)
            layout[index * 3 + 1] = int(gene.c0)
            layout[index * 3 + 2] = int(gene.c1)
            params[index * 3] = float(gene.p0)
            params[index * 3 + 1] = float(gene.p1)
            params[index * 3 + 2] = float(gene.p2)
        base_seed = 0
        if seed is not None:
            base_seed = int(seed) & 0xFFFFFFFFFFFFFFFF
        return self._lib.smcgp_set_genome(self._handle, int(node_id), layout, params,
                                          count, base_seed)

    def _get_genome_native(self, node_id: int) -> Optional[List[Gene]]:
        """从 C 引擎读回图结构。"""
        total = self._lib.smcgp_get_genome(self._handle, int(node_id), None, None, 0)
        if total < 0:
            return None
        if total == 0:
            return []
        layout = (ctypes.c_int32 * (total * 3))()
        params = (ctypes.c_double * (total * 3))()
        self._lib.smcgp_get_genome(self._handle, int(node_id), layout, params, total)
        return [Gene(Op(int(layout[i * 3])), int(layout[i * 3 + 1]), int(layout[i * 3 + 2]),
                     float(params[i * 3]), float(params[i * 3 + 1]), float(params[i * 3 + 2]))
                for i in range(total)]

    def _native_ids(self) -> List[int]:
        """列出 C 引擎里全部神经元 ID。"""
        if not self._handle:
            return []
        total = self._lib.smcgp_list_nodes(self._handle, None, 0)
        if total <= 0:
            return []
        buffer = (ctypes.c_int64 * total)()
        self._lib.smcgp_list_nodes(self._handle, buffer, total)
        return [int(buffer[i]) for i in range(total)]

    def _native_pending_ids(self) -> List[int]:
        """列出下一轮待执行的神经元 ID。"""
        if not self._handle:
            return []
        total = self._lib.smcgp_pending_nodes(self._handle, None, 0)
        if total <= 0:
            return []
        buffer = (ctypes.c_int64 * total)()
        self._lib.smcgp_pending_nodes(self._handle, buffer, total)
        return [int(buffer[i]) for i in range(total)]

    def _native_take_modified(self) -> List[int]:
        """取走自上次落盘以来图结构改过的神经元 ID（C 侧自修改 Python 不知情）。"""
        if not self._handle:
            return []
        total = self._lib.smcgp_take_modified(self._handle, self._id_buffer, self.FIRED_CAP)
        return [int(self._id_buffer[i]) for i in range(min(total, self.FIRED_CAP))]

    # ---------- 持久化 ----------

    def _load_from_db(self) -> None:
        """启动时一次性把图结构读进内存/引擎；运行期不再查库。"""
        with self._db_lock:
            rows = self._db.execute("SELECT id, graph FROM smcgp_nodes").fetchall()
        for node_id, payload in rows:
            try:
                items = json.loads(payload or "[]")
            except ValueError:
                continue
            graph = [
                Gene(Op(int(item["op"])), int(item["c0"]), int(item["c1"]),
                     float(item["p0"]), float(item.get("p1", 0.0)), float(item.get("p2", 0.0)))
                for item in items
            ]
            node_id = int(node_id)
            if self._handle:
                self._set_genome_native(node_id, graph)
                with self._lock:
                    self._known_ids.add(node_id)
            else:
                self.cacheNodes[node_id] = Node(node_id, graph, seed=self.seed)

    def saveNodeToDatabase(self, node_id: int) -> None:
        """把某个神经元标记为待落盘（后台批量写入）。"""
        with self._dirty_lock:
            self._dirty.add(int(node_id))

    def _serialize(self, node_id: int) -> Optional[tuple]:
        """序列化单个神经元（原生模式先读回 C 里的图）。"""
        if self._handle:
            graph = self._get_genome_native(node_id)
            if graph is None:
                return None
        else:
            with self._lock:
                node = self.cacheNodes.get(node_id)
            if node is None:
                return None
            graph = node.graph
        payload = [
            {"op": int(gene.op), "c0": int(gene.c0), "c1": int(gene.c1),
             "p0": float(gene.p0), "p1": float(gene.p1), "p2": float(gene.p2)}
            for gene in graph
        ]
        return node_id, json.dumps(payload, separators=(",", ":"))

    def flush(self, all_nodes: Optional[bool] = None) -> None:
        """把图结构写入数据库（只存图，不存运行时数据）。

        原生模式默认只落盘“图结构改过”的节点（C 侧自修改 Python 不知情，由
        smcgp_take_modified 取回）；关闭网络时用 all_nodes=True 全量落盘。
        纯 Python 模式仍只用待落盘集合。
        """
        with self._flush_lock:
            started = time.monotonic()
            if all_nodes is None:
                all_nodes = False
            if all_nodes:
                ids = set(self._native_ids() if self._handle else self.cacheNodes)
            elif self._handle:
                ids = set(self._native_take_modified())
            else:
                with self._dirty_lock:
                    ids, self._dirty = self._dirty, set()
            rows = []
            for node_id in ids:
                row = self._serialize(node_id)
                if row is not None:
                    rows.append(row)
            if not rows:
                return
            try:
                with self._db_lock:
                    self._db.executemany(
                        "INSERT OR REPLACE INTO smcgp_nodes (id, graph) VALUES (?, ?)", rows)
                    self._db.commit()
                self.save_error = None
                if os.environ.get("BTB_DEBUG") == "1":
                    # 落盘耗时走 stderr：stdout 是 bridge 的纯 JSON 数据流。
                    print(f"[nodeManager] 落盘 {len(rows)} 个节点耗时 "
                          f"{(time.monotonic() - started) * 1000:.1f} ms",
                          file=sys.stderr, flush=True)
            except Exception as error:
                with self._db_lock:
                    self._db.rollback()
                with self._dirty_lock:
                    self._dirty.update(ids)
                self.save_error = str(error)
                raise

    def _write_loop(self) -> None:
        """周期落盘；引擎关闭后自然退出。"""
        while not self._stop.wait(self.save_interval):
            try:
                self.flush()
            except Exception as error:
                self.save_error = str(error)

    def close(self) -> None:
        """强制结束：停止后台线程、把所有神经元图结构落盘后关闭引擎与数据库。"""
        with self._lock:
            if self._closed:
                return
        self._stop.set()
        if self._writer is not None:
            self._writer.join()
        self.flush(all_nodes=True)
        if self._handle:
            self._lib.smcgp_destroy(self._handle)
            self._handle = None
        with self._db_lock:
            self._db.close()
        self._closed = True
