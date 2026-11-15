import collections
import math
import os
import random
import statistics
import sys
import threading
import time

from demo.pong.pong import COLS, ROWS, TICK_MS, Pong, PongWindow
from compute.nodeManager import nodemgr

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
# 四组的 ID 段 = [GROUP_BASE, GROUP_BASE + 4*GROUP_SIZE)，即 4000..7199。
GROUP_END = GROUP_BASE + 4 * GROUP_SIZE
# 刺激神经元：固定 10000 个，占用 10000..19999（不叫 dopamine）。
STIM_SIZE = 10000
STIM_BASE = 10000
# 路由用的 ID 空间 = 10^DPS = 10^5，因此视觉 0..3999、四组 4000..7199 之外，
# 路由还能创建 7200..9999 与 20000..99999 的新神经元（网络可以继续扩张）。
# 刺激神经元 10000..19999 虽然落在路由空间内，但通过 blocked_range 隔离：
# 它们只接受显式激活，别的神经元路由过来的消息一律丢弃。
DPS = 5
SAVE_INTERVAL = None  # None: 只在“强制结束网络并落盘”时全量写入（运行期不落盘，避免卡顿）
# 帧率目标：CPU 没吃满就继续提高输入帧率，不再设置帧率上限。
# 帧长没有硬下限：真实每秒能跑多少拍由每拍耗时决定，迟到处理会把帧长抬回真实可达值，
# 因此这里允许 interval 一路收缩到极小（等价于“能跑多快就跑多快”）。
MIN_FRAME_SECONDS = 0.0
MAX_FRAME_SECONDS = 0.5   # 最低 2 fps
# 帧率自适应间隔：每 1 秒读一次整机 CPU 占用，快速逼近目标帧率。
FPS_ADAPT_INTERVAL = 1.0
# 状态帧最小间隔：控制台只看 Pong/得分/总体数据，最高 60Hz 保证跟手。
STATE_MIN_INTERVAL = 1.0 / 60.0
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


def _stimulation_count(hits):
    """连续触拍 n 次要激活的刺激神经元数量：ceil(10000·n / (3(n+20)))，夹到 [1, 10000]。

    n=1 -> 159，n=2 -> 303，n=20 -> 1667，n=100 -> 2778，上限趋近 3334。
    """
    if hits <= 0:
        return 1
    return max(1, min(STIM_SIZE,
                      int(math.ceil(STIM_SIZE * hits / (3.0 * (hits + 20.0))))))


# 右桨“持续移动”刺激的时间单位：2s。持续移动 x 个单位时，每拍激活 10000/(3x) 个。
MOVE_STIM_UNIT = 2.0


def _movement_stimulation_count(seconds):
    """右桨持续移动 seconds 秒时每拍激活的刺激神经元数：ceil(10000 / (3x))。

    x = 连续真实移动时间（单位 2s，不足 2s 按 1 计）。x=1 -> 3334，x=2 -> 1667，
    x=5 -> 667；停下（或贴边夹住、真实位移为 0）计时归零，再动重新从最大值开始。
    """
    x = max(1.0, float(seconds) / MOVE_STIM_UNIT)
    return max(1, min(STIM_SIZE, int(math.ceil(STIM_SIZE / (3.0 * x)))))


# 触拍/得分的刺激不再“一次打满”，而是登记成刺激包在 STIM_DECAY_TICKS 拍内线性递减到 0。
STIM_DECAY_TICKS = 20

# 右桨“长期在固定地方附近”判定（移动奖励的门槛）：
# 滑动窗口 T 内位置的中位数为 c，若至少 90% 的点满足 |y - c| <= r，
# 认为该时段在固定地方附近；该判定连续成立超过 T 秒 -> 长期固定，暂停移动奖励。
MOVE_FIX_WINDOW = 3.0    # 滑动时间窗口 T（秒）
MOVE_FIX_RADIUS = 2      # 固定位置半径 r（像素）
MOVE_FIX_RATIO = 0.9     # 窗口内需要满足 |y - c| <= r 的比例
# 移动奖励计时对“短暂停顿”的宽限：连续静止超过该秒数才把持续移动计时归零。
# 右桨 1px/帧、很快到边界，若一停就重置，x 会永远停在 1（刺激恒为 3334）。
MOVE_IDLE_RESET = 2.0


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


def _create_manager(db_path, save_interval):
    """创建 SMCGP14 群体：优先 C 引擎（c/smcgp_node.c），没有库时回退纯 Python。

    刺激神经元区间 (STIM_BASE, STIM_SIZE) 设为“拒收路由输入”：它们只响应
    显式 activate（触拍/得分事件），不接受其他神经元的消息。
    """
    return nodemgr(dps=DPS, db_path=db_path, save_interval=save_interval,
                   blocked_range=(STIM_BASE, STIM_SIZE),
                   # 控制台只统计扩展神经元：视觉 0..3999 与四组 4000..7199、
                   # 刺激 10000..19999 都算“保留区间”，不参与激活/计算中统计。
                   reserved_ranges=[(0, GROUP_END), (STIM_BASE, STIM_BASE + STIM_SIZE)])


class PongRuntime:
    def __init__(self, db_path="nodes.db", save_interval=SAVE_INTERVAL):
        self.game = Pong()
        self.manager = _create_manager(db_path, save_interval)
        self._stop_tick = threading.Event()
        self._thread = None
        self._tick_count = 0
        # 刺激神经元状态：连续触拍次数 n、上一次触拍激活量、上次触拍计数、上一次比分。
        self._rally_hits = 0
        self._last_hit_stim = 0
        self._last_hit_events = 0
        self._last_score = (0, 0)
        # 上一次“未计入得分”的出界计数（left, right）：仅用于 DEBUG 观测。
        self._last_discarded = (0, 0)
        # 右桨“持续移动”计时：从开始连续移动的时刻算起；短暂停顿（< MOVE_IDLE_RESET）
        # 不归零，只有连续静止超过 MOVE_IDLE_RESET 秒才重置。
        self._move_started_at = None
        self._move_seconds = 0.0
        self._last_moved_at = 0.0
        # 移动刺激“等级”：移动时等于当前每拍速率；停下后按 step 线性逐拍递减到 0，
        # 避免接到球/停下时移动奖励突然消失（应当像触拍/得分刺激一样慢慢消散）。
        self._movement_level = 0.0
        self._movement_step = 0
        # 右桨位置滑动窗口（判定“长期在固定地方附近”）：元素为 (时间, y)。
        self._move_positions = collections.deque()
        self._fixed_since = None
        self._long_term_fixed = False
        # 触拍/得分的刺激包：[[剩余量, 每拍递减量], ...]，在 STIM_DECAY_TICKS 拍内线性消散。
        self._stim_packets = []
        # 刺激神经元激活统计：本拍累计 / 上一拍实际激活数 / 运行以来累计。
        self._stim_tick = 0
        self._stim_last = 0
        self._stim_total = 0
        # 当前帧长（秒）；start() 每 FPS_ADAPT_INTERVAL 秒按 CPU 占用调整。
        self.interval = TICK_MS / 1000
        # 上一次发出状态帧的时间（高帧率下也不能每拍都建整张网格快照）。
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
                    self.manager.addNode(row * COLS + col)
            labels = {"up": "操作 UP", "down": "操作 DOWN",
                      "sense_up": "感觉 UP", "sense_down": "感觉 DOWN"}
            for name, ids in self.groups.items():
                for index, node_id in enumerate(ids):
                    self.manager.addNode(node_id)
                    self.special_nodes[node_id] = f"{labels[name]} #{index + 1}"
            for node_id in self.groups["up"]:
                self.manager.register_callback(node_id, self._on_up)
            for node_id in self.groups["down"]:
                self.manager.register_callback(node_id, self._on_down)
            # 刺激神经元：固定 10000 个，不参与分组映射，只由触拍/得分事件激活。
            for node_id in range(STIM_BASE, STIM_BASE + STIM_SIZE):
                self.manager.addNode(node_id)
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

    def _stimulate(self, count):
        """随机挑 count 个刺激神经元投递 1.0（同一套 SMCGP14 引擎，不叫 dopamine）。"""
        if count <= 0:
            return 0
        picked = random.sample(range(STIM_BASE, STIM_BASE + STIM_SIZE),
                               min(count, STIM_SIZE))
        activated = int(self.manager.activate_many([(node_id, 1.0) for node_id in picked]))
        # 控制台统计：本拍与累计的刺激神经元激活数（只统计真正被投递成功的节点）。
        self._stim_tick += activated
        self._stim_total += activated
        return activated

    def _update_fixed_state(self, game, now):
        """滑动时间窗口判定右桨是否“长期在固定地方附近”。

        窗口 T=3s 内的位置取中位数 c；若至少 90% 的点满足 |y - c| <= r=2，
        就认为该时段在固定地方附近；该判定连续成立超过 T 秒 -> 长期固定。
        返回 True 表示当前处于“长期固定”（移动奖励应当暂停）。
        """
        self._move_positions.append((now, int(game.right_paddle_y)))
        while self._move_positions and now - self._move_positions[0][0] > MOVE_FIX_WINDOW:
            self._move_positions.popleft()
        positions = [y for _, y in self._move_positions]
        steady = False
        center = 0
        if positions:
            center = statistics.median(positions)
            near = sum(1 for y in positions if abs(y - center) <= MOVE_FIX_RADIUS)
            steady = near >= MOVE_FIX_RATIO * len(positions)
        if not steady:
            if self._fixed_since is not None:
                _debug("离开固定位置附近：恢复移动奖励（此前持续",
                       round(now - self._fixed_since, 2), "s）")
            self._fixed_since = None
            self._long_term_fixed = False
            return False
        if self._fixed_since is None:
            self._fixed_since = now
        fixed_for = now - self._fixed_since
        if not self._long_term_fixed and fixed_for > MOVE_FIX_WINDOW:
            self._long_term_fixed = True
            _debug("判定长期在固定地方附近：中位数", center, "，半径", MOVE_FIX_RADIUS,
                   "，窗口", MOVE_FIX_WINDOW, "s，持续", round(fixed_for, 2),
                   "s -> 暂停移动奖励")
        return self._long_term_fixed

    def _queue_stimulation(self, count):
        """把一次触拍/得分刺激登记成“刺激包”，之后每拍线性递减地激活，慢慢消散。

        每个刺激包的总量 = count，每拍递减 ceil(count / STIM_DECAY_TICKS)，
        因此 count 会在 STIM_DECAY_TICKS 拍内从满额线性降到 0。
        """
        count = int(min(max(count, 0), STIM_SIZE))
        if count <= 0:
            return
        per_tick = max(1, int(math.ceil(count / float(STIM_DECAY_TICKS))))
        self._stim_packets.append([count, per_tick])

    def _drain_stimulation(self):
        """取走所有刺激包本拍的份额；返回本拍要激活的刺激神经元数（线性逐拍递减）。"""
        total = 0
        alive = []
        for remaining, per_tick in self._stim_packets:
            share = min(remaining, per_tick)
            remaining -= share
            total += share
            if remaining > 0:
                alive.append([remaining, per_tick])
        self._stim_packets = alive
        return min(total, STIM_SIZE)

    def _handle_stimulation(self, game):
        """处理触拍与得分事件，并按规则激活刺激神经元。

        - 只有右桨（网络控制的那侧）触拍才算一次“触拍”：连续触拍次数 n += 1，
          登记 ceil(10000·n / (3(n+20))) 个刺激神经元（10000..19999）的刺激包；
        - 只有右玩家得分时，登记“触拍量 3 倍”的刺激包；
        - 触拍/得分的刺激不是一次打满：登记成刺激包后在 STIM_DECAY_TICKS 拍内
          线性逐拍递减地激活，慢慢消散（多个事件会叠加成多个刺激包）；
        - 计分规则：只有“该侧桨最后把球打出去”的失分才计入该侧得分——球从左出界
          且右桨最后触球才算右玩家得分，球从右出界且左桨最后触球才算左玩家得分；
          没人碰过就出界的“白给分”只记到 discarded_* 里，不进比分；
        - 右桨真实移动且未长期固定在原位附近时，每拍给移动刺激：持续移动时间 x
          （单位 2s）越长，激活数越少（10000/(3x)）；
        - **移动刺激停下后不会突然消失**：按与触拍/得分相同的节奏，在
          STIM_DECAY_TICKS 拍内线性逐拍递减到 0（接到球停桨时仍能看到余量）；
        - **移动刺激与触拍/得分刺激包是叠加关系**：同一拍先分别算出份额，再加起来
          一次性投递（不是互相覆盖/替代）；
        - 左玩家得分不刺激；任何一次得分都把连续触拍次数清零。
        - 所有刺激值都是 +1.0，不会投递负值。
        """
        events = game.hit_events - self._last_hit_events
        if events > 0:
            self._last_hit_events = game.hit_events
            for _ in range(events):
                self._rally_hits += 1
                self._last_hit_stim = _stimulation_count(self._rally_hits)
                # 触拍刺激登记成刺激包：之后若干拍线性递减激活，慢慢消散。
                self._queue_stimulation(self._last_hit_stim)
                _debug("第", self._rally_hits, "次右桨触拍，登记刺激包",
                       self._last_hit_stim, "个（", STIM_DECAY_TICKS, "拍内线性消散）")
        discarded = (game.discarded_left, game.discarded_right)
        if discarded != self._last_discarded:
            self._last_discarded = discarded
            _debug("未计入得分的出界：左", discarded[0], "次 / 右", discarded[1],
                   "次（只有该侧桨最后打出去的出界才计分）")
        scores = (game.score_left, game.score_right)
        if scores != self._last_score:
            right_scored = game.score_right > self._last_score[1]
            left_scored = game.score_left > self._last_score[0]
            self._last_score = scores
            if right_scored and self._last_hit_stim > 0:
                reward = min(STIM_SIZE, self._last_hit_stim * 3)
                # 得分刺激同样登记成刺激包，慢慢消散（不一次打满）。
                self._queue_stimulation(reward)
                _debug("右玩家得分（", scores[0], ":", scores[1], "），登记 3 倍刺激包",
                       reward, "个（", STIM_DECAY_TICKS, "拍内线性消散）")
            elif left_scored:
                _debug("左玩家得分（", scores[0], ":", scores[1], "），不激活刺激神经元")
            self._rally_hits = 0
            self._last_hit_stim = 0

        # 右桨移动奖励：先用滑动窗口判断是否“长期在固定地方附近”。
        # 短暂停顿不重置计时（否则 1px/帧 + 边界会让 x 永远停在 1、刺激恒为 3334）；
        # 连续静止超过 MOVE_IDLE_RESET 秒才归零；长期固定时暂停奖励、离开后重置。
        now = time.monotonic()
        long_term_fixed = self._update_fixed_state(game, now)
        moved = game.paddle_moved_up + game.paddle_moved_down
        movement_share = 0
        if moved > 0:
            self._last_moved_at = now
        if long_term_fixed:
            if self._move_started_at is not None:
                _debug("长期固定在原位附近：移动奖励计时归零（本次累计",
                       round(self._move_seconds, 3), "s）")
            self._move_started_at = None
            self._move_seconds = 0.0
            self._movement_level = 0.0
            self._movement_step = 0
        elif moved > 0:
            if self._move_started_at is None:
                self._move_started_at = now
            self._move_seconds = now - self._move_started_at
            level = _movement_stimulation_count(self._move_seconds)
            self._movement_level = float(level)
            # 停下后的消散步长：让移动刺激在 STIM_DECAY_TICKS 拍内线性降到 0。
            self._movement_step = max(1, int(math.ceil(level / float(STIM_DECAY_TICKS))))
            movement_share = level
            _debug("右桨持续移动", round(self._move_seconds, 3), "s（", moved,
                   "像素），移动刺激份额", movement_share, "个")
        else:
            # 没有真实移动：移动刺激不立刻消失，按 step 线性逐拍递减到 0。
            if self._movement_level > 0:
                movement_share = int(math.ceil(self._movement_level))
                self._movement_level = max(0.0, self._movement_level - self._movement_step)
                _debug("移动刺激消散中：本拍", movement_share, "个，剩余",
                       round(self._movement_level, 1))
            if self._move_started_at is not None and now - self._last_moved_at > MOVE_IDLE_RESET:
                _debug("静止超过", MOVE_IDLE_RESET, "s，移动奖励计时归零（本次累计",
                       round(self._move_seconds, 3), "s）")
                self._move_started_at = None
                self._move_seconds = 0.0

        # 触拍/得分的刺激包：本拍取走各自份额（线性逐拍递减），激活后继续残留到后续拍。
        packet_share = self._drain_stimulation()
        # 移动刺激与触拍/得分刺激包是**叠加**关系：本拍合并成一次投递。
        total_share = min(STIM_SIZE, movement_share + packet_share)
        if total_share > 0:
            activated = self._stimulate(total_share)
            _debug("本拍刺激叠加：移动", movement_share, "+ 触拍/得分包", packet_share,
                   "= 计划", total_share, "个（实际", activated, "），剩余包",
                   len(self._stim_packets), "个")

        # 结算本拍刺激统计：本拍累计数转成“上一拍激活数”，然后清零重新累计。
        self._stim_last = self._stim_tick
        self._stim_tick = 0

    def on_tick(self, game):
        """每个游戏节拍：把点亮的像素、感觉输入与刺激事件投给 SMCGP14 群体，然后跑一轮。

        - 视觉：只投递 bit=1 的像素（id = 像素下标），值为 1.0；
        - 感觉：按本拍实际移动格数在对应集群里随机挑节点投递 1.0；
        - 刺激：触拍/得分事件按规则随机激活 10000..19999 的刺激神经元；
        - 每拍跑一轮：节点执行自身 SMCGP14 图（含自修改），输出按目标汇总成下一轮输入；
          操作神经元本轮产生过输出就触发回调，直接驱动右桨。
        """
        started = time.monotonic()
        if self.manager.stopped:
            return
        activate_started = time.monotonic()
        self._handle_stimulation(game)
        delivered = self.manager.activate_visual(game.get_bits())
        sense = []
        for direction, count in (("sense_up", game.paddle_moved_up),
                                 ("sense_down", game.paddle_moved_down)):
            if count:
                for node_id in random.sample(self.groups[direction],
                                             min(count, len(self.groups[direction]))):
                    sense.append((node_id, 1.0))
        if sense:
            delivered += self.manager.activate_many(sense)
        activate_ms = (time.monotonic() - activate_started) * 1000.0
        pump_started = time.monotonic()
        fired = self.manager.pump_events()
        pump_ms = (time.monotonic() - pump_started) * 1000.0
        self._tick_count += 1
        now = time.monotonic()
        emit_ms = 0.0
        if self.on_state is not None and now - self._last_snapshot >= STATE_MIN_INTERVAL:
            # 状态帧绑定在节拍线程里发出，但按最小间隔节流：帧率再高也不会每拍建 4000 格快照。
            self._last_snapshot = now
            emit_started = time.monotonic()
            self.game_state = self._game_snapshot()
            try:
                self.on_state(self.game_state)
            except Exception as error:
                # stdout 断开（网页/server 被强制关闭）时不要让节拍线程崩溃。
                _debug("状态帧发送失败", error)
            emit_ms = (time.monotonic() - emit_started) * 1000.0
        if DEBUG and self._tick_count % 40 == 0:
            _debug("节拍", self._tick_count, "投递", delivered, "输入，触发", fired, "节点，耗时",
                   round((time.monotonic() - started) * 1000, 2), "ms",
                   "activate", round(activate_ms, 2), "pump", round(pump_ms, 2),
                   "emit", round(emit_ms, 2))

    def _game_snapshot(self):
        """生成一帧游戏快照（Pong 画面 + 得分 + 本拍操作计数 + 刺激激活统计）。"""
        game = self.game
        return {
            "grid": game.get_grid(), "rows": ROWS, "cols": COLS,
            "score_left": game.score_left, "score_right": game.score_right,
            "up": game.commanded_up, "down": game.commanded_down,
            "moved_up": game.paddle_moved_up, "moved_down": game.paddle_moved_down,
            # 互斥丢弃的 UP/DOWN 指令数（观测/调试用）。
            "blocked_up": game.blocked_up, "blocked_down": game.blocked_down,
            # 是否被判定为“长期在固定地方附近”（1/0，观测/调试用）。
            "fixed_still": 1 if self._long_term_fixed else 0,
            # 刺激神经元激活数：上一拍实际激活 / 运行以来累计。
            "stim_active": self._stim_last, "stim_total": self._stim_total,
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
        """按整机 CPU 占用调节输入帧率：没吃满就加速，接近吃满就降速，不设帧率上限。"""
        busy = self._cpu_busy_ratio()
        previous_fps = self.fps
        if busy is not None:
            if busy < 0.7:
                self.interval = max(MIN_FRAME_SECONDS, self.interval / 1.6)
            elif busy > 0.92:
                self.interval = min(MAX_FRAME_SECONDS, self.interval * 1.25)
        _debug("帧率自适应：CPU 忙碌", None if busy is None else round(busy, 3),
               round(previous_fps, 1), "fps ->", round(self.fps, 1), "fps")

    def start(self):
        def loop():
            interval = self.interval
            next_at = time.monotonic() + interval
            next_adapt = time.monotonic() + FPS_ADAPT_INTERVAL
            late_streak = 0
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
                    lateness = now - next_at
                    # 偶发的一次迟到（图线程抢 GIL）只丢掉这一拍，不降低帧率；
                    # 连续迟到才说明当前帧长真的喂不动，按迟到量放宽。
                    late_streak += 1
                    if late_streak >= 3:
                        interval = min(MAX_FRAME_SECONDS, max(interval, lateness * 1.2))
                        self.interval = interval
                        late_streak = 0
                    next_at = now
                else:
                    late_streak = 0
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
