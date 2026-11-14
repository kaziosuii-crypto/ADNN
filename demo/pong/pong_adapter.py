import os
import random
import sys
import threading
import time

from demo.pong.pong import COLS, ROWS, TICK_MS, Pong, PongWindow
from compute.mp_manager import MPNodeManager

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
# 自适应帧率：每 180 秒按“最慢单个神经元耗时”重新适应一次。
ADAPT_INTERVAL = 180.0
# 帧长上下限：避免最慢神经元只有几十微秒时把游戏和网页淹没，也避免极慢时画面停滞。
MIN_FRAME_SECONDS = 0.02  # 最高 50 fps
MAX_FRAME_SECONDS = 0.5   # 最低 2 fps
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


class PongRuntime:
    def __init__(self, db_path="nodes.db", save_interval=SAVE_INTERVAL):
        self.game = Pong()
        grid = self.game.get_grid()
        self.manager = MPNodeManager(0, grid[0][0], DPS, TIME_WINDOW, db_path, save_interval)
        self._stop_tick = threading.Event()
        self._thread = None
        self._tick_count = 0
        # 当前帧长（秒）；start() 里每 ADAPT_INTERVAL 秒按最慢神经元耗时更新。
        self.interval = TICK_MS / 1000
        # 每拍结束后的状态回调（bridge 用它把 state 帧直接绑到游戏节拍上，帧率随自适应帧长变化）。
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
            used = set(manager.cacheNodes) | set(range(GROUP_BASE + 4))
            for node in manager.cacheNodes.values():
                used.update(node.forwards)
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
        # 整帧视觉输入一次性打包，避免 4000 次跨进程 put。
        batch = [((row * COLS + col), grid[row][col])
                 for row in range(ROWS) for col in range(COLS)]
        for direction, count in (("sense_up", game.paddle_moved_up),
                                 ("sense_down", game.paddle_moved_down)):
            for node_id in random.sample(self.groups[direction], min(count, len(self.groups[direction]))):
                batch.append((node_id, 1))
        self.manager.activate_many(batch)
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
        self.game_state = self._game_snapshot()
        self._tick_count += 1
        if self.on_state is not None:
            # 状态帧绑定在节拍线程里发出：有几拍就有几帧，不会像独立定时器那样越跑越慢。
            try:
                self.on_state(self.game_state)
            except Exception as error:
                # stdout 断开（网页/server 被强制关闭）时不要让节拍线程崩溃。
                _debug("状态帧发送失败", error)
        if DEBUG and self._tick_count % 40 == 0:
            _debug("节拍", self._tick_count, "投递", len(batch), "节点，耗时",
                   round((time.monotonic() - started) * 1000, 2), "ms")

    def _game_snapshot(self):
        game = self.game
        return {
            "grid": game.get_grid(), "rows": ROWS, "cols": COLS,
            "score_left": game.score_left, "score_right": game.score_right,
            "up": game.commanded_up, "down": game.commanded_down,
            "moved_up": game.paddle_moved_up, "moved_down": game.paddle_moved_down,
        }

    @property
    def fps(self):
        """当前自适应帧率（帧/秒），供 bridge 展示与调试。"""
        return 1.0 / self.interval if self.interval > 0 else 0.0

    def start(self):
        def loop():
            # 帧长自适应：一帧的现实长度 = 最慢单个神经元计算耗时；
            # 每 ADAPT_INTERVAL 秒重新采样一次，期间沿用上一轮的帧长。
            interval = self.interval
            next_at = time.monotonic() + interval
            next_adapt = time.monotonic() + ADAPT_INTERVAL
            while not self._stop_tick.is_set():
                if self._stop_tick.wait(max(0.0, next_at - time.monotonic())):
                    break
                if self.manager.stopped:
                    break
                now = time.monotonic()
                if now >= next_adapt:
                    slowest_ms = self.manager.take_slowest_ms()
                    if slowest_ms > 0:
                        interval = min(MAX_FRAME_SECONDS, max(MIN_FRAME_SECONDS, slowest_ms / 1000.0))
                    self.interval = interval
                    _debug("自适应帧率：最慢神经元", round(slowest_ms, 3), "ms ->",
                           round(1.0 / interval, 2), "fps")
                    next_adapt = now + ADAPT_INTERVAL
                self.game.update()
                self.on_tick(self.game)
                next_at += interval
                # 某一拍明显超时后重新对齐，避免进入追赶式忙循环。
                now = time.monotonic()
                if next_at < now:
                    next_at = now
        self._thread = threading.Thread(target=loop, name="pong-tick", daemon=True)
        self._thread.start()

    def close(self):
        self.manager.request_stop()
        self._stop_tick.set()
        if self._thread:
            self._thread.join()
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
