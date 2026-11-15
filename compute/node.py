"""SMCGP14 神经元：一个能在运行期修改自身程序的独立计算单元。

设计约定（本文件只实现中性的自修改计算机制）：
- 内部计算图用有序 list 存放，每个内部节点是 g = (f, c0, c1, p0, p1, p2)；
- 连接采用相对地址：第 i 个节点读取第 (i - c) 个节点，越界/前向引用一律读作 0；
- 14 种指令：INP/OUTPUT/DADD/DMULT/CONST/IFLTE/INDX + 7 种自修改指令；
- 一轮内"读取旧图、轮末提交新图"，只有被 OUTPUT 追溯到的节点才会被执行；
- 不实现 sigmoid、权重更新、dopamine、奖励筛选等任何额外学习机制；
- 只保存图结构（JSON），运行时数据（输入、输出、修改队列）不落盘。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, List, Sequence


class Op(IntEnum):
    """SMCGP14 的 14 种指令。"""
    INP = 0          # 读取分配给它的外部输入
    OUTPUT = 1       # 产生一条输出消息
    DADD = 2         # a + b
    DMULT = 3        # a * b
    CONST = 4        # 返回 p0
    IFLTE = 5        # a < 0 时返回 b，否则返回 0
    INDX = 6         # 返回自身在图中的位置
    ADD = 7          # 自修改：插入随机节点
    DEL = 8          # 自修改：删除区间
    MOV = 9          # 自修改：移动区间
    CHC = 10         # 自修改：改连接
    CHF = 11         # 自修改：改指令类型
    FLR = 12         # 自修改：清空待提交队列
    CHP = 13         # 自修改：改参数


# 7 种自修改指令；触发条件统一为 a >= b。
SELF_MODIFY_OPS = frozenset({Op.ADD, Op.DEL, Op.MOV, Op.CHC, Op.CHF, Op.FLR, Op.CHP})

# ---- 固定的边界规则（写死在这里并被单元测试锁定）----
# 单条 ADD 一轮最多插入的随机节点数，避免自修改爆图。
MAX_ADD_GENES = 32
# 随机初始化参数范围（与设计文档示例一致，仅用于新建内部节点）。
RANDOM_RANGE = 16


@dataclass
class Gene:
    """神经元内部 SMCGP 计算图的一个节点：g = (f, c0, c1, p0, p1, p2)。"""
    op: Op
    c0: int = 1
    c1: int = 1
    p0: float = 0.0
    p1: float = 0.0
    p2: float = 0.0


@dataclass(frozen=True)
class Emission:
    """OUTPUT 产生的输出消息：信号值 + 目标地址编码（由外层 nodeManager 解释）。"""
    value: float
    destination: float


def distribute_inputs(values: Sequence[float], count: int) -> List[float]:
    """把本轮收到的数值分配给 count 个 INP。

    规则（固定并单测覆盖）：
    - count <= len(values)：按浮点均分边界取重叠窗口，每个窗口取平均值；
    - count > len(values)：按环形重复补齐（values[k % n]）。

    示例：[0.2,0.4,0.6,0.8] -> 2 个 INP => [0.3, 0.7]；
          [0.2,0.4,0.6]     -> 2 个 INP => [0.3, 0.5]；
          [0.2,0.4]         -> 3 个 INP => [0.2, 0.4, 0.2]。
    """
    if count <= 0:
        return []
    values = [float(item) for item in values]
    if not values:
        return [0.0] * count
    total = len(values)
    if count <= total:
        distributed = []
        for slot in range(count):
            # 浮点均分边界，再向外取整成重叠窗口：起点 floor、终点 ceil。
            low = int(total * slot / count)
            high = int(-(-total * (slot + 1) // count))  # ceil
            window = values[low:max(high, low + 1)]
            distributed.append(sum(window) / len(window))
        return distributed
    return [values[slot % total] for slot in range(count)]


class Node:
    """SMCGP14 神经元：执行内部图、处理自修改、保存/加载图结构。

    对外最小接口：
        node = Node(node_id, graph, seed)
        emissions = node.run(inputs)
        node.save(path) / Node.load(path)
    """

    def __init__(self, node_id: int, graph: Iterable[Gene] | None = None, seed=None):
        """创建一个神经元；未提供图时按随机种子图生成（保证至少一个 INP 与一个 OUTPUT）。"""
        self.id = int(node_id)
        self.rng = random.Random(seed)
        self.graph: List[Gene] = list(graph) if graph is not None else self._seed_graph()
        # 完整自修改日志（实验观测用，不参与计算）。
        self.modification_log: List[dict] = []
        self._round = 0
        # 以下都是"本轮临时数据"，每轮开始清空。
        self._cache: dict[int, float] = {}
        self._busy: set[int] = set()
        self._input_slots: dict[int, int] = {}
        self._input_values: List[float] = []
        self._modifications: List[tuple] = []

    # ---------- 对外接口 ----------

    def run(self, inputs: Iterable[float]) -> List[Emission]:
        """执行一轮 SMCGP14：分配输入、计算当前图、提交自修改、返回输出消息。"""
        self._round += 1
        self._cache = {}
        self._busy = set()
        self._modifications = []
        self._input_values = self._distribute_inputs(list(inputs))
        self._input_slots = self._collect_input_slots()
        # 只有 OUTPUT 会驱动计算：按图顺序遍历，未参与有效计算的节点不会执行。
        emissions: List[Emission] = []
        for index, gene in enumerate(self.graph):
            if gene.op is Op.OUTPUT:
                value = self._eval_link(index, gene.c0)
                destination = self._eval_link(index, gene.c1)
                emissions.append(Emission(value, destination))
        self._commit()
        return emissions

    def _distribute_inputs(self, values: List[float]) -> List[float]:
        """把本轮收到的信号分配给当前图里的所有 INP（数量每轮现算）。"""
        count = sum(1 for gene in self.graph if gene.op is Op.INP)
        return distribute_inputs(values, count)

    def save(self, path: str) -> None:
        """只把图结构写成 JSON，便于直接检查神经元发育出的结构。"""
        payload = {
            "id": self.id,
            "graph": [
                {"op": int(gene.op), "c0": int(gene.c0), "c1": int(gene.c1),
                 "p0": float(gene.p0), "p1": float(gene.p1), "p2": float(gene.p2)}
                for gene in self.graph
            ],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str, seed=None) -> "Node":
        """从磁盘恢复图结构；运行时数据（输入、输出、修改队列）不恢复。"""
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        graph = [
            Gene(Op(int(item["op"])), int(item["c0"]), int(item["c1"]),
                 float(item["p0"]), float(item.get("p1", 0.0)), float(item.get("p2", 0.0)))
            for item in payload.get("graph", [])
        ]
        return cls(payload.get("id", 0), graph, seed=seed)

    # ---------- 内部执行 ----------

    def _collect_input_slots(self) -> dict:
        """建立 "内部节点位置 -> 第几个 INP 槽位" 的映射。"""
        slots = {}
        slot = 0
        for index, gene in enumerate(self.graph):
            if gene.op is Op.INP:
                slots[index] = slot
                slot += 1
        return slots

    def _eval(self, index: int) -> float:
        """求某个内部节点的值（本轮缓存、迭代求值）；越界/前向引用/环一律返回 0。"""
        if index in self._cache:
            return self._cache[index]
        stack = [(index, False)]
        while stack:
            current, expanded = stack.pop()
            if current in self._cache:
                continue
            if current < 0 or current >= len(self.graph):
                self._cache[current] = 0.0
                continue
            gene = self.graph[current]
            if not expanded:
                if current in self._busy:
                    # 环：直接把该节点压成 0，避免无限递归。
                    self._cache[current] = 0.0
                    continue
                self._busy.add(current)
                stack.append((current, True))
                for dep in self._deps(current, gene):
                    if dep not in self._cache and dep not in self._busy:
                        stack.append((dep, False))
                continue
            self._busy.discard(current)
            self._cache[current] = self._compute(current, gene)
        return self._cache.get(index, 0.0)

    @staticmethod
    def _deps(index: int, gene: Gene) -> tuple:
        """返回某节点计算前需要求值的内部节点位置（相对连接换算成绝对位置）。

        c < 1 表示自引用或前向引用，按非法连接处理：不产生依赖，读取时返回 0。
        """
        if gene.op in (Op.DADD, Op.DMULT, Op.IFLTE, Op.OUTPUT) or gene.op in SELF_MODIFY_OPS:
            return tuple(index - int(link) for link in (gene.c0, gene.c1) if int(link) >= 1)
        return ()

    def _read(self, index: int, link: int) -> float:
        """读取相对连接指向的前方节点；c < 1（自引用/前向引用）一律读作 0。"""
        if int(link) < 1:
            return 0.0
        return self._cache.get(index - int(link), 0.0)

    def _eval_link(self, index: int, link: int) -> float:
        """先按需计算相对连接指向的前方节点，再返回其值；非法连接返回 0。"""
        if int(link) < 1:
            return 0.0
        return self._eval(index - int(link))

    def _compute(self, index: int, gene: Gene) -> float:
        """按指令语义计算一个节点的值；自修改指令在这里登记修改请求。"""
        op = gene.op
        if op is Op.INP:
            slot = self._input_slots.get(index)
            if slot is None or slot >= len(self._input_values):
                return 0.0
            return float(self._input_values[slot])
        if op is Op.CONST:
            return float(gene.p0)
        if op is Op.INDX:
            return float(index)
        if op is Op.DADD or op is Op.DMULT:
            a = self._read(index, gene.c0)
            b = self._read(index, gene.c1)
            return a + b if op is Op.DADD else a * b
        if op is Op.IFLTE:
            a = self._read(index, gene.c0)
            b = self._read(index, gene.c1)
            return b if a < 0 else 0.0
        if op is Op.OUTPUT:
            # OUTPUT 的返回值本身只用于参与计算，消息在 run() 里单独收集。
            return self._read(index, gene.c0)
        if op in SELF_MODIFY_OPS:
            a = self._read(index, gene.c0)
            b = self._read(index, gene.c1)
            if a >= b:
                # 触发：登记修改请求，返回 a；未触发则返回 b。
                self._modifications.append((index, op, float(gene.p0), float(gene.p1), float(gene.p2)))
                return a
            return b
        return 0.0

    def _commit(self) -> None:
        """按登记顺序提交自修改请求（本轮计算全部结束后才动图结构）。"""
        queue = self._modifications
        self._modifications = []
        position = 0
        while position < len(queue):
            index, op, p0, p1, p2 = queue[position]
            position += 1
            if op is Op.FLR:
                # FLR 清空尚未提交的其余请求，本身不改变图结构。
                self._log(index, op, p0, p1, p2)
                queue.clear()
                break
            if self._apply(index, op, p0, p1, p2):
                self._log(index, op, p0, p1, p2)

    def _log(self, index: int, op: Op, p0: float, p1: float, p2: float) -> None:
        """记录一条已提交的自修改，便于事后回溯图结构变化。"""
        self.modification_log.append({
            "round": self._round,
            "op": op.name,
            "index": index,
            "p0": p0, "p1": p1, "p2": p2,
            "graph_len": len(self.graph),
        })

    # ---------- 7 种自修改的具体边界行为 ----------

    def _apply(self, index: int, op: Op, p0: float, p1: float, p2: float) -> bool:
        """执行一条自修改；返回是否真的改变了图结构（越界/无效修改返回 False）。"""
        size = len(self.graph)
        if op is Op.ADD:
            # 插入位置 = 当前节点位置 + p0（夹到 [0, len]），数量 = p1（0..MAX_ADD_GENES）。
            start = _clamp(index + int(p0), 0, size)
            count = _clamp(int(p1), 0, MAX_ADD_GENES)
            if count <= 0:
                return False
            self.graph[start:start] = [self._random_gene() for _ in range(count)]
            return True
        if op is Op.DEL:
            # 删除区间 [当前+ p0, +p1)，左闭右开；越界自动夹紧。
            start = _clamp(index + int(p0), 0, size)
            end = _clamp(start + int(p1), start, size)
            if end <= start:
                return False
            del self.graph[start:end]
            return True
        if op is Op.MOV:
            # 取出区间 [当前+p0, +p1)，再插入到 当前+p2（按删除后的图长度夹紧）。
            start = _clamp(index + int(p0), 0, size)
            length = _clamp(int(p1), 0, size - start)
            if length <= 0:
                return False
            block = self.graph[start:start + length]
            del self.graph[start:start + length]
            target = _clamp(index + int(p2), 0, len(self.graph))
            self.graph[target:target] = block
            return True
        if op is Op.CHC:
            # 目标 = 当前 + p0；p1 % 2 选 c0 或 c1；新连接值下限为 1（只允许引用前方节点）。
            target = _clamp(index + int(p0), 0, size - 1)
            if size <= 0:
                return False
            new_link = max(1, int(p2))
            if int(p1) % 2 == 0:
                self.graph[target].c0 = new_link
            else:
                self.graph[target].c1 = new_link
            return True
        if op is Op.CHF:
            # 目标指令 = p1 % 14，允许 14 种类型之间任意转换（不保护 INP/OUTPUT）。
            target = _clamp(index + int(p0), 0, size - 1)
            if size <= 0:
                return False
            self.graph[target].op = Op(int(p1) % len(Op))
            return True
        if op is Op.CHP:
            # 目标参数编号 = p1 % 3，新参数值 = p2。
            target = _clamp(index + int(p0), 0, size - 1)
            if size <= 0:
                return False
            which = int(p1) % 3
            value = float(p2)
            if which == 0:
                self.graph[target].p0 = value
            elif which == 1:
                self.graph[target].p1 = value
            else:
                self.graph[target].p2 = value
            return True
        return False

    def _seed_graph(self) -> List[Gene]:
        """新建神经元的初始图：INP + 1~4 个随机节点 + 一个随机回连的 OUTPUT。

        保证每个新神经元至少能收一次输入、发一次输出（否则整张网络会退化成
        “所有节点都发往同一个 ID”的死结构）；中间节点仍由 random_gene 生成，
        因此 INP/OUTPUT 与 7 种自修改指令都可能出现在初始图里。
        """
        middle = self.rng.randint(1, 4)
        graph = [Gene(Op.INP)]
        graph.extend(self._random_gene() for _ in range(middle))
        size = middle + 2
        graph.append(Gene(Op.OUTPUT, self.rng.randint(1, size - 1),
                          self.rng.randint(1, size - 1)))
        return graph

    def _random_gene(self) -> Gene:
        """为 ADD 生成一个随机内部节点：14 种指令全部可能被生成。"""
        return Gene(
            op=Op(self.rng.randrange(len(Op))),
            c0=self.rng.randint(1, RANDOM_RANGE),
            c1=self.rng.randint(1, RANDOM_RANGE),
            p0=self.rng.uniform(-RANDOM_RANGE, RANDOM_RANGE),
            p1=self.rng.uniform(-RANDOM_RANGE, RANDOM_RANGE),
            p2=self.rng.uniform(-RANDOM_RANGE, RANDOM_RANGE),
        )


def _clamp(value: int, low: int, high: int) -> int:
    """把整数夹到 [low, high]；high 小于 low 时返回 low。"""
    if high < low:
        return low
    return low if value < low else (high if value > high else value)
