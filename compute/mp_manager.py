import heapq
import json
import math
import multiprocessing as mp
import os
import queue
import random
import sqlite3
import sys
import threading
import time

# 注：不再使用 mpmath。节点值本身以 float64 存库，DPS=4 时 float64 的有效精度远超需求，
# 而 mpmath 是纯 Python 大数运算，每个激活/开火都要在 GIL 下跑几千条字节码，是主要热点之一。

# 内存上限 100G，由父进程与所有 worker 共同分摊。
MEMORY_LIMIT = 100 * 1024 ** 3
DEFAULT_WORKERS = 28
# 收集结果、序列化、发网页固定用 2 个核。
CHANNELS = 2
# 每个节点用自己的时钟衰减：每秒一步，每步减 0.1；没有全局衰减节拍。
DECAY_INTERVAL = 1.0
DECAY_STEP = 0.1
# worker 一次处理多大一批到期节点再回去读命令。命令在每批结束后立刻排水，
# 所以加大批次只减少“读 dopamine / 判断 stop”这类跨进程读的次数，不会饿死新激活。
WORK_BATCH = 1024
# worker 空闲时最多睡这么久。新命令到达时 multiprocessing 的管道会直接唤醒 get()，
# 不依赖这个超时来发现新任务，所以不必缩到 0.01（28 个 worker 空转会白白烧 CPU）。
WORK_IDLE_WAIT = 0.25
# worker 内的事件先攒成一批再跨进程发送，减少管道 put 次数。
EVENT_BATCH = 64
WORKER_JOIN_TIMEOUT = 60.0
# 强制结束网络时给已被 terminate 的 worker 留的回收时间（秒）。
TERMINATE_JOIN_TIMEOUT = 5.0

# 大量排查输出：设置环境变量 BTB_DEBUG=1 即可看到运行期细分日志。
DEBUG = os.environ.get("BTB_DEBUG") == "1"


def _debug(*args):
    """统一的调试输出入口，默认关闭，避免污染 stdout 的 JSON 数据流。"""
    if DEBUG:
        print("[mp_manager]", *args, file=sys.stderr, flush=True)
# 落盘时每个事务的行数。
FLUSH_CHUNK = 5000
# 父进程处理回调时默认不等待事件通道追平：节点算完就继续走，不再固定阻塞游戏线程。
# 需要同步语义的调用方可以显式传入 timeout。
PUMP_TIMEOUT = 0.0


class NodeView:
    def __init__(self, node_id, value, dps, window, forwards=None):
        self.id = node_id
        self.value = float(value)
        self.dps = dps
        self.timeWindow = window
        self.forwards = {int(key): value for key, value in (forwards or {}).items()}
        self.active = False

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
    deadlines = []
    decays = []
    decay_at = {}
    # 自适应帧率采样：记录本批内单个神经元（到期分支）最长的一次计算耗时。
    slowest_elapsed = 0.0
    slowest_node_id = -1
    started = time.monotonic()
    for node in nodes.values():
        if node.forwards:
            decay_at[node.id] = started + DECAY_INTERVAL
            heapq.heappush(decays, (decay_at[node.id], node.id))

    def get_node(node_id, value=None):
        if node_id not in nodes:
            nodes[node_id] = NodeView(node_id, node_id / 10 ** dps if value is None else value, dps, window)
        return nodes[node_id]

    # 每条通道一个待发列表；事件按目标节点 ID 取模分到两条通道，保持同一节点的先后顺序。
    pending = [[], []]

    def emit(kind, data):
        channel = int(data[0]) % CHANNELS
        pending[channel].append((kind, worker_id, data))
        if len(pending[channel]) >= EVENT_BATCH:
            events[channel].put(("batch", worker_id, pending[channel]))
            pending[channel] = []

    def flush_events():
        """把攒下的事件一次性交给父进程；每批到期节点处理完就调用一次。"""
        for channel in range(CHANNELS):
            if not pending[channel]:
                continue
            events[channel].put(("batch", worker_id, pending[channel]))
            pending[channel] = []

    def schedule_decay(node):
        # 节点只在第一次产生连接时登记自己的衰减时钟，之后按自身节奏滚动。
        if node.id in decay_at or not node.forwards:
            return
        at = time.monotonic() + DECAY_INTERVAL
        decay_at[node.id] = at
        heapq.heappush(decays, (at, node.id))

    def activate_node(node_id, value):
        """激活单个节点：S 形累加、登记窗口、上报瘦身事件并登记衰减时钟。"""
        node = get_node(node_id)
        node.value = 1 / (1 + math.exp(-(node.value + value)))
        if not node.active:
            node.active = True
            heapq.heappush(deadlines, (time.monotonic() + window, node.id))
        # 瘦身激活事件：只回传 id / value / active。出边在激活时不会变化，
        # 若照旧回传整份 snapshot，视觉组每拍 4000 次激活都要跨进程复制出边字典。
        emit("active", (node.id, float(node.value), True))
        schedule_decay(node)

    def handle(command):
        kind = command[0]
        if kind == "activate":
            activate_node(command[1], command[2])
        elif kind == "activate_many":
            # 同一拍里同属该 worker 的一批激活，逐条走相同的激活路径。
            for node_id, value in command[1]:
                activate_node(node_id, value)
        elif kind == "add":
            get_node(command[1], command[2])
        elif kind == "stop":
            return False
        return True

    _debug("worker", worker_id, "启动，节点数", len(nodes))
    try:
        running = True
        while running:
            handled = 0
            # dopamine / stop 都是跨进程对象，每读一次都要走一次 IPC，按批只读一次。
            level = float(dopamine.value)
            stopped_now = stop.is_set()
            while handled < WORK_BATCH and not stopped_now:
                now = time.monotonic()
                if deadlines and deadlines[0][0] <= now:
                    # 计时范围覆盖“单神经元从到期待处理到 fan-out 投递完成”的全部现实耗时。
                    node_started = time.monotonic()
                    _, node_id = heapq.heappop(deadlines)
                    node = nodes[node_id]
                    target = int(math.sin(math.pi * node.value) * 10 ** dps)
                    if level > 0:
                        target += int(random.uniform(-1, 1) * math.sqrt(level) * 10 ** dps)
                    weight = node.forwards.get(target, 0) + 1
                    node.forwards[target] = weight
                    node.active = False
                    # 只回传“新增/加强的那一条边”：父进程直接写入同一目标即可。
                    # 以前这里回传整份 forwards 字典，视觉组每拍开火 4000 次就要跨进程 pickle 4000 个字典。
                    emit("fire", (node.id, float(node.value), False, int(target), float(weight)))
                    value_out = float(node.value)
                    # 同一节点的一次开火可能命中多个 worker，按目标 worker 合并成一次 activate_many，
                    # 避免为每一条出边单独做一次跨进程管道写入（原来 100 条边就是 100 次 put）。
                    to_activate = {}
                    for target_id, connection in node.forwards.items():
                        if random.random() < min(1.0, connection):
                            to_activate.setdefault(target_id % count, []).append((target_id, value_out))
                    for target_worker, pairs in to_activate.items():
                        commands[target_worker].put(("activate_many", pairs))
                    # 回调事件只需要节点 ID：父进程收到后从自己的缓存里取节点，
                    # 之前重复回传整份 snapshot 等于把同一个字典发了两遍。
                    emit("callback", (node_id,))
                    schedule_decay(node)
                    handled += 1
                    elapsed = time.monotonic() - node_started
                    if elapsed > slowest_elapsed:
                        slowest_elapsed = elapsed
                        slowest_node_id = node_id
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
                        # 衰减只回传节点 ID：父进程用完全相同的规则（权重 -0.1、≤0.1 丢弃）本地复现。
                        # 原来每秒给 6 万节点各 pickle 一份出边字典，父子进程都在做无意义的序列化。
                        emit("decay", (node.id,))
                    if node.forwards:
                        decay_at[node_id] = at + DECAY_INTERVAL
                        heapq.heappush(decays, (decay_at[node_id], node_id))
                    else:
                        decay_at.pop(node_id, None)
                else:
                    break
            if slowest_elapsed > 0.0:
                # 上报本批最慢单神经元耗时；data[0] 用 worker_id 以满足 emit 的取模分通道规则。
                emit("timing", (worker_id, slowest_node_id, slowest_elapsed * 1000.0))
                slowest_elapsed = 0.0
                slowest_node_id = -1
            flush_events()
            # 每批到期节点之后立刻清一小批命令，任何到期节点都不会独占 worker。
            for _ in range(WORK_BATCH):
                try:
                    command = commands[worker_id].get_nowait()
                except queue.Empty:
                    break
                if not handle(command):
                    running = False
                    break
            flush_events()
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
            # 空闲时阻塞等命令：最多睡 WORK_IDLE_WAIT，新命令到达会立刻唤醒；
            # 若最近有节点时钟到期，则按到期时间提前醒来。
            next_at = min(deadlines[0][0] if deadlines else float("inf"),
                          decays[0][0] if decays else float("inf"))
            timeout = max(0.0, min(WORK_IDLE_WAIT, next_at - time.monotonic()))
            try:
                command = commands[worker_id].get(timeout=timeout)
            except queue.Empty:
                continue
            if not handle(command):
                break
    finally:
        # 退出前把最后一批事件送出去，避免网页丢失收尾状态。
        try:
            flush_events()
        except Exception:
            pass
        try:
            _save_partition(db_path, nodes, cache_kib, save_lock)
        finally:
            # 停止时父进程可能已不再读取队列；取消后台发送线程的 join，避免进程无法退出。
            for channel in events:
                channel.cancel_join_thread()
            commands[worker_id].cancel_join_thread()
        _debug("worker", worker_id, "退出")


def _payload_part(nodes):
    """把节点及其出边压成网页需要的紧凑数组，不加节点锁以降低序列化开销。"""
    rows, edges = [], []
    for node in nodes:
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
        # 落盘用的脏集合与增量图用的脏集合必须分开：flush() 会清空前者。
        self._dirty = set()
        # 结构脏集合：新节点、出边集合发生增减的节点；增量帧只为这些节点附带边数据。
        self._graph_dirty = set()
        # 激活状态脏集合：只有 active 真正翻转的节点才需要推给网页，
        # 数值微调与重复激活不再触发整帧重发，避免每 0.25s 推送数万节点与全部出边。
        self._active_dirty = set()
        # O(1) 统计：节点数、激活数、连接数在 _apply/addNode 时增量维护。
        self._node_count = 0
        self._active_count = 0
        self._edge_count = 0
        self._callbacks_pending = []
        # 自适应帧率：记录自上次取值以来最慢的单神经元计算耗时（毫秒）。
        self._timing_lock = threading.Lock()
        self._slowest_ms = 0.0
        self._slowest_id = -1
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
            # 启动载入时一次性把计数建好，后续只做增量维护。
            self._node_count += 1
            self._edge_count += len(edges)
            partitions[node_id % self.worker_count].append((node_id, value, edges))
        _debug("载入数据库节点数", self._node_count, "连接数", self._edge_count)
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
        """激活单个节点，按分片直接投递给对应 worker。"""
        with self._lock:
            if not self.stopped:
                self._commands[int(node_id) % self.worker_count].put(("activate", int(node_id), float(value)))

    def activate_many(self, pairs):
        """把同一拍里的激活按 worker 分组，每个 worker 只收一次跨进程消息。

        pairs 是 (node_id, value) 的可迭代对象；同一批里的顺序不影响激活语义。
        """
        if self.stopped:
            return
        grouped = {}
        for node_id, value in pairs:
            node_id = int(node_id)
            grouped.setdefault(node_id % self.worker_count, []).append((node_id, float(value)))
        for worker_id, items in grouped.items():
            self._commands[worker_id].put(("activate_many", items))
        _debug("activate_many 共", sum(len(items) for items in grouped.values()),
               "条，分成", len(grouped), "个 worker 批次")

    def addNode(self, node_id, value):
        node_id = int(node_id)
        with self._lock:
            node = self.cacheNodes.get(node_id)
            if node is None:
                node = NodeView(node_id, value, self.initdps, self.initTimeWindow)
                self.cacheNodes[node_id] = node
                self._node_count += 1
                self._edge_count += len(node.forwards)
                with self._dirty_lock:
                    self._dirty.add(node_id)
                    self._graph_dirty.add(node_id)
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

    def _apply_fire(self, node_id, value, active, target, weight):
        """合并 worker 的开火事件（只带新增/加强的那一条边）。

        两个消费者线程会并发调用，计数和脏集合都在 _dirty_lock 内更新。
        新增目标键才算结构变化；同一目标重复加强只更新权重，不触发图增量重发。
        """
        target = int(target)
        node = self.cacheNodes.get(node_id)
        with self._dirty_lock:
            if node is None:
                node = NodeView(node_id, value, self.initdps, self.initTimeWindow)
                self.cacheNodes[node_id] = node
                self._node_count += 1
                old_active = False
            else:
                old_active = node.active
            if old_active:
                self._active_count -= 1
            if active:
                self._active_count += 1
            new_edge = target not in node.forwards
            node.value = float(value)
            node.active = bool(active)
            node.forwards[target] = float(weight)
            if new_edge:
                self._edge_count += 1
                self._graph_dirty.add(node_id)
            self._dirty.add(node_id)
            if old_active != bool(active):
                self._active_dirty.add(node_id)

    def _apply_decay(self, node_id):
        """合并 worker 的衰减事件（只带节点 ID），用与 worker 完全相同的规则本地复现。

        规则：每条出边权重减 DECAY_STEP，权重不再大于 DECAY_STEP 的边直接删除。
        这样每秒 6 万次衰减不再需要把出边字典序列化一遍跨进程搬运。
        """
        with self._dirty_lock:
            node = self.cacheNodes.get(node_id)
            if node is None:
                return
            old_edges = len(node.forwards)
            node.forwards = {target: weight - DECAY_STEP
                             for target, weight in node.forwards.items() if weight > DECAY_STEP}
            removed = old_edges - len(node.forwards)
            if removed:
                self._edge_count -= removed
                self._graph_dirty.add(node_id)
            self._dirty.add(node_id)

    def _apply_active(self, node_id, value, active):
        """合并 worker 上报的瘦身激活事件（只带 id / value / active，不带出边）。

        激活不会修改出边，所以这里不再复制 forwards；图增量只在 active 翻转时标记，
        避免视觉组每拍 4000 次激活都携带整份出边字典穿越进程管道。
        """
        node = self.cacheNodes.get(node_id)
        with self._dirty_lock:
            if node is None:
                node = NodeView(node_id, value, self.initdps, self.initTimeWindow)
                self.cacheNodes[node_id] = node
                self._node_count += 1
                old_active = False
                self._graph_dirty.add(node_id)
            else:
                old_active = node.active
            if old_active:
                self._active_count -= 1
            if active:
                self._active_count += 1
            node.value = float(value)
            node.active = bool(active)
            self._dirty.add(node_id)
            if old_active != bool(active):
                self._active_dirty.add(node_id)

    def _handle(self, event):
        """处理 worker 事件；一个批次里可能包含很多条，递归展开后逐条合并。"""
        kind, worker_id, data = event
        if kind == "batch":
            for item in data:
                self._handle(item)
        elif kind == "fire":
            # data = (node_id, value, active, target, weight)：只描述新增/加强的那一条边。
            self._apply_fire(int(data[0]), float(data[1]), bool(data[2]),
                             int(data[3]), float(data[4]))
        elif kind == "decay":
            # data = (node_id,)：衰减内容由父进程按同一规则本地复现。
            self._apply_decay(int(data[0]))
        elif kind == "active":
            # 瘦身激活事件：data = (node_id, value, active)，不含出边。
            self._apply_active(int(data[0]), float(data[1]), bool(data[2]))
        elif kind == "callback":
            with self._callback_lock:
                self._callbacks_pending.append(data[0])
        elif kind == "timing":
            # data = (worker_id, node_id, 毫秒)：只保留采样窗口内最慢的一条。
            elapsed = float(data[2])
            with self._timing_lock:
                if elapsed > self._slowest_ms:
                    self._slowest_ms = elapsed
                    self._slowest_id = int(data[1])

    def take_slowest_ms(self):
        """取出并清零自上次调用以来观测到的最慢单神经元耗时（毫秒）。

        PongRuntime 每 180 秒调用一次，用这个值重算游戏帧长；返回 0 表示窗口内没有样本。
        """
        with self._timing_lock:
            elapsed = self._slowest_ms
            node_id = self._slowest_id
            self._slowest_ms = 0.0
            self._slowest_id = -1
        _debug("取最慢神经元", node_id, "耗时", round(elapsed, 3), "ms")
        return elapsed

    def pump_events(self, timeout=None):
        """把已到达的事件回调交给调用线程执行，默认零等待（算完就继续走）。

        timeout=None 使用 PUMP_TIMEOUT（当前为 0）；timeout>0 时最多等待事件通道追平，
        供需要“本帧事件全部落地”的调用方使用。回调仍在调用线程里跑，不跨线程改游戏状态。
        """
        if self._closed:
            return 0
        wait = PUMP_TIMEOUT if timeout is None else float(timeout)
        if wait > 0:
            deadline = time.monotonic() + wait
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
        _debug("pump_events 处理回调", count, "个")
        return count

    def summary(self):
        """返回统计信息；计数是增量维护的，这里只做 O(1) 读取。"""
        self.pump_events()
        with self._dirty_lock:
            nodes = self._node_count
            active = self._active_count
            edges = self._edge_count
        return {"stopped": self.stopped, "saved": self._closed, "dopamine": self.dopamine,
                "save_error": self.save_error, "nodes": nodes,
                "active": active, "edges": edges}

    def _graph_nodes(self, pump=True):
        """取当前缓存节点快照；pump=False 时不再等待事件追平（供独立图线程使用）。"""
        if pump:
            self.pump_events()
        with self._lock:
            return list(self.cacheNodes.values())

    def graph_payload(self, pump=True):
        nodes = self._graph_nodes(pump)
        parts = [None, None]

        def build(index, chunk):
            parts[index] = _payload_part(chunk)

        _split_build(nodes, build)
        rows, edges = [], []
        for part in parts:
            rows.extend(part[0])
            edges.extend(part[1])
        # 全量帧已包含所有变化，清空增量集合，避免紧接着再发一遍重复节点。
        with self._dirty_lock:
            self._graph_dirty.clear()
            self._active_dirty.clear()
        return {"nodes": rows, "edges": edges}

    def graph_json(self, pump=True):
        """在 2 个核上并行完成节点/连线的序列化，返回可直接写入网页的 JSON 文本。"""
        started = time.monotonic()
        nodes = self._graph_nodes(pump)
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
        with self._dirty_lock:
            self._graph_dirty.clear()
            self._active_dirty.clear()
        _debug("graph_json 全量", len(nodes), "节点，耗时",
               round((time.monotonic() - started) * 1000, 2), "ms")
        return '{"nodes":[' + node_text + '],"edges":[' + edge_text + ']}'

    def graph_patch_json(self):
        """只序列化自上一帧以来有变化的节点及其出边；没有变化时返回 None。

        帧里分两类：
          - nodes：激活状态翻转过、或结构变化的节点（只带 id/active/value）；
          - edgeNodes + edges：出边集合真正发生增减的节点，只有它们的边是权威数据。
        前端只能按 edgeNodes 更新出边表，否则会把“仅状态变化”的节点的边误删。
        注意：这里清空的是 _graph_dirty / _active_dirty，与落盘用的 _dirty 完全独立，
        因此异步 flush 不会把增量图数据提前清掉。
        """
        with self._dirty_lock:
            structural, self._graph_dirty = self._graph_dirty, set()
            toggles, self._active_dirty = self._active_dirty, set()
        touched = structural | toggles
        if not touched:
            return None
        with self._lock:
            nodes = [self.cacheNodes[node_id] for node_id in touched if node_id in self.cacheNodes]
            edge_nodes = [self.cacheNodes[node_id] for node_id in structural if node_id in self.cacheNodes]
        started = time.monotonic()
        rows = [[node.id, int(node.active), round(float(node.value), 6)] for node in nodes]
        edges = []
        for node in edge_nodes:
            edges.extend([node.id, int(target), round(float(weight), 4)]
                         for target, weight in node.forwards.items())
        edge_ids = [node.id for node in edge_nodes]
        _debug("graph_patch_json 增量", len(rows), "节点，", len(edge_ids), "个节点带边，",
               len(edges) // 3, "边，耗时", round((time.monotonic() - started) * 1000, 2), "ms")
        return ('{"nodes":' + json.dumps(rows, separators=(",", ":"))
                + ',"edges":' + json.dumps(edges, separators=(",", ":"))
                + ',"edgeNodes":' + json.dumps(edge_ids, separators=(",", ":")) + '}')

    def flush(self):
        with self._dirty_lock:
            dirty, self._dirty = self._dirty, set()
        if not dirty:
            return
        with self._lock:
            nodes = [self.cacheNodes[node_id] for node_id in dirty if node_id in self.cacheNodes]
        rows = [(node.id, float(node.value), json.dumps(node.forwards)) for node in nodes]
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

    def _force_terminate_workers(self):
        """强制终止全部 worker 进程，跳过它们各自的收尾落盘与 5 秒排水。

        旧的结束链路依赖每个 worker 在 finally 里写自己的分片，但 worker 只认识
        分给自己的节点，无法覆盖父进程缓存里的全集；这里直接杀掉进程，
        后续由父进程一次性写库，避免两边同时写同一批行。
        """
        alive = 0
        for process in self._processes:
            if process.is_alive():
                alive += 1
                try:
                    process.terminate()
                except Exception as error:
                    _debug("worker 强制终止异常", process.pid, repr(error))
        for process in self._processes:
            try:
                process.join(timeout=TERMINATE_JOIN_TIMEOUT)
            except Exception as error:
                _debug("worker 回收异常", process.pid, repr(error))
            if process.is_alive():
                # Windows 上 kill 与 terminate 等价，这里只是兜底再杀一次。
                try:
                    process.kill()
                except Exception as error:
                    _debug("worker 兜底 kill 异常", process.pid, repr(error))
                process.join()
        remaining = sum(1 for process in self._processes if process.is_alive())
        _debug("强制终止 worker", alive, "个，仍存活", remaining, "个")

    def _drain_events(self):
        """把事件队列里残留的 worker 快照并入父进程缓存（不等待、不重复消费）。

        消费者线程已经 join，这里可以安全地独占队列；worker 已终止，
        管道里剩下的就是它们退出前最后发出的状态。
        """
        drained = 0
        for channel in range(CHANNELS):
            events = self._events[channel]
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                except Exception as error:
                    _debug("收尾读取事件异常", channel, repr(error))
                    break
                try:
                    self._handle(event)
                except Exception as error:
                    _debug("收尾合并事件异常", channel, repr(error))
                drained += 1
        _debug("收尾合并残留事件", drained, "条")
        return drained

    def _collect_all_states(self):
        """递归收集全部节点状态，不判断节点是否激活。

        以父进程缓存为根，沿每个节点的 forwards 目标 ID 深度优先展开；
        目标节点尚未在缓存中实例化时，按与 worker 完全一致的默认初值补建
        NodeView（id / 10 ** dps），保证“有连接但还没跑过”的目标也落盘。
        """
        created = 0
        with self._lock:
            seen = {}
            stack = list(self.cacheNodes.values())
            for node in stack:
                seen[node.id] = node
            while stack:
                node = stack.pop()
                for target in list(node.forwards.keys()):
                    target_id = int(target)
                    if target_id in seen:
                        continue
                    target_node = self.cacheNodes.get(target_id)
                    if target_node is None:
                        target_node = NodeView(target_id, target_id / 10 ** self.initdps,
                                               self.initdps, self.initTimeWindow)
                        self.cacheNodes[target_id] = target_node
                        self._node_count += 1
                        created += 1
                    seen[target_id] = target_node
                    stack.append(target_node)
            nodes = list(seen.values())
        _debug("递归收集节点", len(nodes), "个，补建未实例化目标", created, "个")
        return nodes

    def _save_all_rows(self, nodes):
        """把给定节点的全量状态分块写入 nodes 表并提交。

        逐块生成行数据再提交，避免一次性构造百万级 rows 列表带来的额外内存峰值。
        """
        started = time.monotonic()
        written = 0
        with self._db_lock:
            for start in range(0, len(nodes), FLUSH_CHUNK):
                chunk = nodes[start:start + FLUSH_CHUNK]
                rows = [(node.id, float(node.value), json.dumps(node.forwards)) for node in chunk]
                self._db.executemany(
                    "INSERT OR REPLACE INTO nodes (id, value, forwards) VALUES (?, ?, ?)",
                    rows,
                )
                self._db.commit()
                written += len(rows)
        _debug("全量落盘", written, "行，耗时", round((time.monotonic() - started) * 1000, 2), "ms")

    def save_all_and_stop(self):
        """结束网络并落盘：不判断激活状态，递归保存全部节点后立即强制结束网络。

        与旧的 close() 链路不同，这里不再等 worker 自己写分片，也不再走 5 秒排水：
        1. 置停止标志，阻止新的激活与回调；
        2. 停掉后台定时落盘线程；
        3. 强制终止全部 worker（它们退出时的分片写入被直接跳过）；
        4. 停掉事件消费者，手动清空队列并合并最后一批快照；
        5. 递归收集全部节点（不区分 active）后全量写库；
        6. 关闭数据库与队列，标记已结束。
        """
        with self._close_lock:
            if self._closed:
                _debug("网络已关闭，跳过重复落盘")
                return
            started = time.monotonic()
            # 1. 阻止新的激活：activate / activate_many / addNode 都会先看这个标志。
            with self._lock:
                self._stop.set()
            # 2. 停掉定时落盘，避免它与全量写库争同一把数据库锁。
            self._writer_stop.set()
            if self._writer:
                self._writer.join()
            # 3. 直接强制终止所有 worker。
            self._worker_stop.set()
            self._force_terminate_workers()
            # 4. 消费者停下后独占队列，把残留快照并入缓存。
            self._consumer_stop.set()
            for consumer in self._consumers:
                consumer.join()
            self._drain_events()
            # 5. 递归收集全部节点并一次性写库。
            nodes = self._collect_all_states()
            try:
                self._save_all_rows(nodes)
                self.save_error = None
            except Exception as error:
                # 即使落盘失败也要把网络强制结束，错误只记录下来供界面展示。
                self.save_error = str(error)
                _debug("全量落盘失败", repr(error))
            # 6. 收尾：关闭数据库与所有跨进程队列。
            with self._db_lock:
                try:
                    self._db.close()
                except Exception as error:
                    _debug("关闭数据库异常", repr(error))
            with self._pump_lock:
                self._closed = True
                # worker 已终止，没人再读这些队列；取消 join，避免解释器退出时卡住。
                for channel in self._events:
                    channel.cancel_join_thread()
                    channel.close()
                for command in self._commands:
                    command.cancel_join_thread()
                    command.close()
            _debug("结束网络并落盘完成：节点", len(nodes), "耗时",
                   round((time.monotonic() - started) * 1000, 2), "ms")

    def close(self):
        """结束网络并落盘按钮的统一入口：递归保存全部节点后强制结束网络。"""
        self.save_all_and_stop()


nodemgr = MPNodeManager
