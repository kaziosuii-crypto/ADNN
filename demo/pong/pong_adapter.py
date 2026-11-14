import os
import random
import sys
import threading
import time

from demo.pong.pong import COLS, ROWS, TICK_MS, Pong, PongWindow
from compute.mp_manager import MPNodeManager
from compute.native_manager import NativeNodeManager, load_native_library

# 视觉神经元数量 = ROWS * COLS = 4000。
VISUAL_SIZE = ROWS * COLS
# 操作/感觉四组固定各 800 个，不随视觉尺寸变化；总神经元数保持 3200。
GROUP_SIZE = 800
# 组 ID 从视觉区间之后开始分配（4000..4003 为四组 seed）。
GROUP_BASE = VISUAL_SIZE
ID_UP        = GROUP_BASE       # 4000
ID_DOWN      = GROUP_BASE + 1   # 4001
ID_SENSE_UP  = GROUP_BASE + 2   # 4002
ID_SENSE_DOWN = GROUP_BASE + 3  # 4003
DPS = 4
TIME_WINDOW = 2.0  # seconds (2000 ms)
SAVE_INTERVAL = 2.0  # None: save only when stopping the network
# 帧率目标：CPU 没吃满就提高输入帧率，最高 240fps。
MAX_FPS = 240
MIN_FRAME_SECONDS = 1.0 / MAX_FPS
MAX_FRAME_SECONDS = 0.5   # 最低 2 fps
# 帧率自适应间隔：每 3 秒读一次整机 CPU 占用，比 180 秒更快接近目标帧率。
FPS_ADAPT_INTERVAL = 3.0
# 状态帧最小间隔：240fps 下也给网页留出渲染余量（最高 25 状态帧/秒）。
STATE_MIN_INTERVAL = 0.04
# 大量排查输出：BTB_DEBUG=1 时在 stderr 打印每拍的耗时与投递量。
DEBUG = os.environ.get("BTB_DEBUG") == "1"


def _debug(*args):
    """诊断输出走 stderr，避免污染 bridge 的 JSON 数据流。"""
    if DEBUG:
        print("[pong_adapter]", *args, file=sys.stderr, flush=True)
GROUP_STARTS = {
    "up": ID_UP, "down": ID_DOWN,
    "sense_up": ID_SENSE_UP, "sense_down": ID_SENSE_DOWN,
}


def _cpu_times():
    """读取 /proc/stat 的第一行，返回 (总时间, 空闲时间)；非 Linux 返回 None。"""
    try:
        with open("/proc/stat", "r") as handle:
            fields = handle.readline().split()
        values = [int(item) for item in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle
    except (OSError, ValueError, IndexError):
        return None


def _worker_count():
    """分片/进程数：优先 BTB_WORKERS，否则用整机核数。"""
    override = os.environ.get("BTB_WORKERS")
    if override:
        return max(1, int(override))
    return max(1, os.cpu_count() or 28)


def _create_manager(initvalue, db_path, save_interval):
    """按 BTB_ENGINE 选择引擎：auto 优先原生 C 引擎，缺失时回退多进程实现。"""
    mode = os.environ.get("BTB_ENGINE", "auto").strip().lower()
    if mode in ("auto", "c", "native"):
        if load_native_library() is not None:
            try:
                return NativeNodeManager(0, initvalue, DPS, TIME_WINDOW, db_path, save_interval,
                                         worker_count=_worker_count(), visual_size=VISUAL_SIZE)
            except Exception as error:
                if mode != "auto":
                    raise
                _debug("原生引擎初始化失败，回退多进程：", error)
        elif mode in ("c", "native"):
            raise RuntimeError("BTB_ENGINE=c，但找不到 c/btb_core 原生库（先编译）")
    return MPNodeManager(0, initvalue, DPS, TIME_WINDOW, db_path, save_interval)


class PongRuntime:
    def __init__(self, db_path="nodes.db", save_interval=SAVE_INTERVAL):
        self.game = Pong()
        grid = self.game.get_grid()
        self.manager = _create_manager(grid[0][0], db_path, save_interval)
        self._stop_tick = threading.Event()
        self._thread = None
        self._tick_count = 0
        # 当前帧长（秒）；start() 每 FPS_ADAPT_INTERVAL 秒按 CPU 占用调整。
        self.interval = TICK_MS / 1000
        # 上一次发出状态帧的时间（240fps 下也不能每拍都建整张网格快照）。
        self._last_snapshot = 0.0
        # CPU 采样基线：用于计算整机忙碌比例。
        self._cpu_prev = None
        # 每拍结束后的状态回调（bridge 用它把 state 帧直接绑到游戏节拍上）。
        self.on_state = None
        try:
            self.groups = self._load_groups()
            self.special_nodes = {
                row * COLS + col: f"视觉 ({row}, {col})"
                for row in range(ROWS) for col in range(COLS)
            }
            for row in range(ROWS):
                for col in range(COLS):
                    self.manager.addNode(row * COLS + col, grid[row][col])
            labels = {"up": "操作 UP", "down": "操作 DOWN",
                      "sense_up": "感觉 UP", "sense_down": "感觉 DOWN"}
            for name, ids in self.groups.items():
                for index, node_id in enumerate(ids):
                    self.manager.addNode(node_id, 0.5)
                    self.special_nodes[node_id] = f"{labels[name]} #{index + 1}"
            for node_id in self.groups["up"]:
                self.manager.register_callback(node_id, self._on_up)
            for node_id in self.groups["down"]:
                self.manager.register_callback(node_id, self._on_down)
            self.game_state = self._game_snapshot()
        except Exception:
            self.manager.close()
            raise

    def _load_groups(self):
        """为四组操作/感觉神经元分配 ID，并保留已落盘的分组映射。

        视觉输入固定占用 0..VISUAL_SIZE-1，组 ID 从 GROUP_BASE 起分配。
        每组容量固定 GROUP_SIZE（=800），不随视觉尺寸变化。
        旧版本把组放在 800..3999，与新的视觉区间冲突，这里检测到旧映射后清表重建。
        """
        manager = self.manager
        with manager._db_lock:
            db = manager._db
            db.execute(
                "CREATE TABLE IF NOT EXISTS pong_groups "
                "(name TEXT NOT NULL, position INTEGER NOT NULL, node_id INTEGER NOT NULL UNIQUE, "
                "PRIMARY KEY (name, position))"
            )
            existing = db.execute(
                "SELECT name, position, node_id FROM pong_groups ORDER BY position"
            ).fetchall()
            stale = [row for row in existing if row[2] < GROUP_BASE]
            if stale:
                # 旧分组 ID 落在视觉区间内，直接迁移：清空映射后按新地址段重新分配。
                _debug("检测到", len(stale), "条旧版分组映射（node_id <", GROUP_BASE, "），清表重建")
                db.execute("DELETE FROM pong_groups")
                existing = []
            groups = {name: [start] for name, start in GROUP_STARTS.items()}
            for name, position, node_id in existing:
                if name not in groups or position != len(groups[name]):
                    raise ValueError("Invalid persisted Pong group mapping")
                groups[name].append(node_id)
            # 已用 ID = 现有节点 + 所有出边目标；引擎无关，由管理器提供。
            used = manager.used_ids() | set(range(GROUP_BASE + 4))
            for ids in groups.values():
                used.update(ids)
            candidates = (node_id for node_id in range(GROUP_BASE + 4, 10 ** DPS + 1)
                          if node_id not in used)
            additions = []
            for name, ids in groups.items():
                if len(ids) > GROUP_SIZE:
                    raise ValueError("Persisted Pong group is larger than the fixed group size")
                while len(ids) < GROUP_SIZE:
                    try:
                        node_id = next(candidates)
                    except StopIteration:
                        raise ValueError("Not enough unused node IDs for the Pong groups") from None
                    additions.append((name, len(ids), node_id))
                    ids.append(node_id)
            db.executemany(
                "INSERT INTO pong_groups (name, position, node_id) VALUES (?, ?, ?)",
                additions,
            )
            db.commit()
        _debug("分组就绪：每组", GROUP_SIZE, "个，起始 ID", GROUP_BASE)
        return groups

    def _on_up(self, node):
        self.game.move_right_up(1)

    def _on_down(self, node):
        self.game.move_right_down(1)

    def on_tick(self, game):
        """每个游戏节拍收集一批激活，按 worker 分组后一次性投递。

        视觉网格每帧整片同步激活；感觉节点按实际移动距离在集群内随机挑选。
        保留每个神经元独立的激活窗口与线程天然时间差，不做统一调度。
        pump_events() 现在零等待：只处理已到达的回调，计算侧不会阻塞游戏节拍。
        """
        started = time.monotonic()
        self.manager.pump_events()
        if self.manager.stopped:
            return
        grid = game.get_grid()
        activate_visual = getattr(self.manager, "activate_visual", None)
        if callable(activate_visual):
            # 原生引擎：只把 4000 位的视觉位图交给 C，各分片线程自己展开并计算自己的神经元。
            bits = bytes(1 if grid[row][col] else 0
                         for row in range(ROWS) for col in range(COLS))
            activate_visual(bits)
            sense = []
            for direction, count in (("sense_up", game.paddle_moved_up),
                                     ("sense_down", game.paddle_moved_down)):
                if count:
                    for node_id in random.sample(self.groups[direction],
                                                 min(count, len(self.groups[direction]))):
                        sense.append((node_id, 1))
            if sense:
                self.manager.activate_many(sense)
            delivered = len(bits)
        else:
            # 多进程实现：整帧视觉输入一次性打包，避免 4000 次跨进程 put。
            batch = [((row * COLS + col), grid[row][col])
                     for row in range(ROWS) for col in range(COLS)]
            for direction, count in (("sense_up", game.paddle_moved_up),
                                     ("sense_down", game.paddle_moved_down)):
                for node_id in random.sample(self.groups[direction], min(count, len(self.groups[direction]))):
                    batch.append((node_id, 1))
            self.manager.activate_many(batch)
            delivered = len(batch)
        # 新的 dopamine 规则：
        #   右玩家（神经网络控制）失分 -> dopamine +1；
        #   左玩家失分 -> dopamine <= 0 时再 -1，否则直接置 -1。
        out_side = game.out_side
        if out_side == "right":
            self.manager.add_dopamine(1)
            _debug("右玩家失分，dopamine =", round(self.manager.dopamine, 4))
        elif out_side == "left":
            if self.manager.dopamine <= 0:
                self.manager.add_dopamine(-1)
            else:
                self.manager.dopamine = -1
            _debug("左玩家失分，dopamine =", round(self.manager.dopamine, 4))
        if out_side is not None:
            game.out_side = None
            game.ball_out = False
        # 保留原有设计：右桨击中球时 dopamine 归零。
        if game.paddle_hit:
            self.manager.dopamine = 0
            game.paddle_hit = False
        self._tick_count += 1
        now = time.monotonic()
        if self.on_state is not None and now - self._last_snapshot >= STATE_MIN_INTERVAL:
            # 状态帧绑定在节拍线程里发出，但按最小间隔节流：240fps 下也不会每拍建 4000 格快照。
            self._last_snapshot = now
            self.game_state = self._game_snapshot(grid)
            try:
                self.on_state(self.game_state)
            except Exception as error:
                # stdout 断开（网页/server 被强制关闭）时不要让节拍线程崩溃。
                _debug("状态帧发送失败", error)
        if DEBUG and self._tick_count % 40 == 0:
            _debug("节拍", self._tick_count, "投递", delivered, "节点，耗时",
                   round((time.monotonic() - started) * 1000, 2), "ms")

    def _game_snapshot(self, grid=None):
        game = self.game
        return {
            "grid": game.get_grid() if grid is None else grid, "rows": ROWS, "cols": COLS,
            "score_left": game.score_left, "score_right": game.score_right,
            "up": game.commanded_up, "down": game.commanded_down,
            "moved_up": game.paddle_moved_up, "moved_down": game.paddle_moved_down,
        }

    @property
    def fps(self):
        """当前自适应帧率（帧/秒），供 bridge 展示与调试。"""
        return 1.0 / self.interval if self.interval > 0 else 0.0

    def _cpu_busy_ratio(self):
        """返回自上次采样以来整机 CPU 的忙碌比例（0~1）；拿不到 /proc/stat 时返回 None。"""
        current = _cpu_times()
        if current is None:
            return None
        previous = self._cpu_prev
        self._cpu_prev = current
        if previous is None:
            return None
        total = current[0] - previous[0]
        idle = current[1] - previous[1]
        if total <= 0:
            return None
        return 1.0 - idle / total

    def _adapt_fps(self):
        """按整机 CPU 占用调节输入帧率：没吃满就加速，接近吃满就降速，上限 240fps。"""
        busy = self._cpu_busy_ratio()
        previous_fps = self.fps
        if busy is not None:
            if busy < 0.7:
                self.interval = max(MIN_FRAME_SECONDS, self.interval / 1.3)
            elif busy > 0.92:
                self.interval = min(MAX_FRAME_SECONDS, self.interval * 1.3)
        _debug("帧率自适应：CPU 忙碌", None if busy is None else round(busy, 3),
               round(previous_fps, 1), "fps ->", round(self.fps, 1), "fps")

    def start(self):
        def loop():
            interval = self.interval
            next_at = time.monotonic() + interval
            next_adapt = time.monotonic() + FPS_ADAPT_INTERVAL
            while not self._stop_tick.is_set():
                if self._stop_tick.wait(max(0.0, next_at - time.monotonic())):
                    break
                if self.manager.stopped:
                    break
                now = time.monotonic()
                if now >= next_adapt:
                    self._adapt_fps()
                    interval = self.interval
                    next_adapt = now + FPS_ADAPT_INTERVAL
                self.game.update()
                tick_started = time.monotonic()
                self.on_tick(self.game)
                elapsed = time.monotonic() - tick_started
                next_at += interval
                now = time.monotonic()
                if next_at < now:
                    # 一拍超时：当前帧长喂不动，立刻放宽帧长并把基准对齐到当前时刻，
                    # 避免进入“追赶式”忙循环把 CPU 全烧在空转上。
                    interval = min(MAX_FRAME_SECONDS, max(interval * 1.25, elapsed * 1.1))
                    self.interval = interval
                    next_at = now
        self._thread = threading.Thread(target=loop, name="pong-tick", daemon=True)
        self._thread.start()

    def close(self):
        """结束网络并落盘：先停游戏节拍，再走强制结束路径。"""
        # 先停节拍线程，保证关闭期间不再向 worker 投递新的激活命令。
        self._stop_tick.set()
        if self._thread:
            self._thread.join()
        # manager.close() 现在等价于强制路径：递归保存全部节点后立刻终止网络，
        # 不再等待各 worker 各自收尾落盘，也不给 worker 发 stop 命令。
        self.manager.close()


def run(on_up_activated=None, on_down_activated=None, root=None, on_manager_ready=None):
    runtime = PongRuntime()
    try:
        if on_manager_ready:
            on_manager_ready(runtime.manager, runtime.special_nodes)
        PongWindow(game=runtime.game, on_tick=runtime.on_tick, root=root)
    finally:
        runtime.close()


def main():
    run()


if __name__ == '__main__':
    main()
