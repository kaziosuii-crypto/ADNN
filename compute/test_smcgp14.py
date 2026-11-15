"""SMCGP14 神经元与群体层的边界单测（只跑纯计算，不启动任何服务）。

覆盖设计文档要求固定的边界：
- 相对连接的读取范围（越界 / 自引用 / 前向引用一律读作 0）；
- 输入分配的三条示例；
- 7 种自修改的区间起止（左闭右开）与参数取模规则；
- FLR 只作废其后尚未提交的请求；
- 图结构保存/加载、随机种子可复现；
- 群体层的外层寻址、消息汇总与落盘重载。
"""

import math
import os
import tempfile
import unittest

from compute.node import Gene, Node, Op, add_gene_count, distribute_inputs
from compute.nodeManager import load_smcgp_library, nodemgr


def g(op, c0=1, c1=1, p0=0.0, p1=0.0, p2=0.0):
    """构造一个内部节点的简写。"""
    return Gene(op, c0, c1, p0, p1, p2)


class DistributeInputsTest(unittest.TestCase):
    """输入分配规则必须在各种数量关系下固定。"""

    def test_more_values_than_inputs(self):
        self._assert_close(distribute_inputs([0.2, 0.4, 0.6, 0.8], 2), [0.3, 0.7])

    def test_overlapping_window(self):
        self._assert_close(distribute_inputs([0.2, 0.4, 0.6], 2), [0.3, 0.5])

    def _assert_close(self, actual, expected):
        """浮点均值按误差比较，避免 0.30000000000000004 这类表示差异。"""
        self.assertEqual(len(actual), len(expected))
        for got, want in zip(actual, expected):
            self.assertAlmostEqual(got, want, places=12)

    def test_more_inputs_than_values(self):
        self.assertEqual(distribute_inputs([0.2, 0.4], 3), [0.2, 0.4, 0.2])

    def test_no_input_and_no_value(self):
        self.assertEqual(distribute_inputs([0.2], 0), [])
        self.assertEqual(distribute_inputs([], 2), [0.0, 0.0])


class NodeEvaluateTest(unittest.TestCase):
    """14 种指令的计算语义。"""

    def test_relative_sum(self):
        # 位置 2 的 DADD：c0=2 读位置 0，c1=1 读位置 1。
        node = Node(1, [g(Op.CONST, p0=2), g(Op.CONST, p0=3),
                        g(Op.DADD, c0=2, c1=1), g(Op.OUTPUT, c0=1, c1=1)])
        emissions = node.run([])
        self.assertEqual([(item.value, item.destination) for item in emissions], [(5.0, 5.0)])

    def test_out_of_range_reads_zero(self):
        node = Node(1, [g(Op.DADD, c0=99, c1=99), g(Op.OUTPUT, c0=1, c1=1)])
        self.assertEqual(node.run([])[0].value, 0.0)

    def test_forward_link_reads_zero(self):
        # c0=-1 表示指向后面的节点，属于非法连接，读作 0。
        node = Node(1, [g(Op.DADD, c0=-1, c1=-1), g(Op.CONST, p0=7),
                        g(Op.OUTPUT, c0=2, c1=1)])
        emission = node.run([])[0]
        self.assertEqual(emission.value, 0.0)
        self.assertEqual(emission.destination, 7.0)

    def test_dmult_and_iflte(self):
        node = Node(1, [g(Op.CONST, p0=-5), g(Op.CONST, p0=9),
                        g(Op.DMULT, c0=2, c1=1), g(Op.IFLTE, c0=1, c1=2),
                        g(Op.OUTPUT, c0=1, c1=1)])
        # IFLTE 的 a = DMULT(-5 * 9) = -45 < 0 => 返回 b = 9。
        self.assertEqual(node.run([])[0].value, 9.0)

    def test_indx_returns_position(self):
        node = Node(1, [g(Op.CONST, p0=0), g(Op.INDX), g(Op.OUTPUT, c0=1, c1=1)])
        self.assertEqual(node.run([])[0].value, 1.0)

    def test_no_input_node_reads_zero(self):
        node = Node(1, [g(Op.OUTPUT, c0=1, c1=1)])
        emission = node.run([5.0])[0]
        self.assertEqual((emission.value, emission.destination), (0.0, 0.0))

    def test_no_output_means_no_side_effect(self):
        node = Node(1, [g(Op.CONST, p0=0), g(Op.ADD, p0=0, p1=4)])
        self.assertEqual(node.run([1.0]), [])
        self.assertEqual(len(node.graph), 2)


class NodeSelfModifyTest(unittest.TestCase):
    """7 种自修改的边界行为（左闭右开、越界夹紧、参数取模）。"""

    def _run_trigger(self, graph, inputs=None):
        """构造含触发节点的图并跑一轮；返回同一个 Node。"""
        node = Node(1, graph, seed=7)
        node.run([] if inputs is None else inputs)
        return node

    def test_add_inserts_genes(self):
        # 触发值 a=0 -> 16·sin(0)=0 -> 数量夹到最小 1。
        node = self._run_trigger([g(Op.CONST), g(Op.ADD, p0=0),
                                  g(Op.OUTPUT, c0=1, c1=1)])
        self.assertEqual(len(node.graph), 4)
        self.assertEqual(node.modification_log[0]["op"], "ADD")

    def test_add_count_follows_sine_of_trigger(self):
        """ADD 数量 = clamp(round(|16·sin(a)|), 1, 16)，a 是触发值，不再由 p1 决定。"""
        def inserted(value):
            node = self._run_trigger([g(Op.CONST, p0=value), g(Op.ADD, p0=0),
                                      g(Op.OUTPUT, c0=1, c1=1)])
            return len(node.graph) - 3
        self.assertEqual(inserted(0.0), 1)             # 16·sin(0)=0 -> 夹到 1
        self.assertEqual(inserted(math.pi / 6), 8)     # 16·0.5=8
        self.assertEqual(inserted(math.pi / 2), 16)    # 16·1=16
        self.assertEqual(inserted(-math.pi / 2), 16)   # 取绝对值
        self.assertEqual(inserted(10.0), add_gene_count(10.0))

    def test_add_stops_at_max_genes(self):
        """单神经元基因上限：ADD 最多长到上限，超出部分丢弃；之后不再增长。"""
        graph = [g(Op.CONST) for _ in range(19)] + [g(Op.CONST, p0=math.pi / 2),
                                                    g(Op.ADD, p0=0),
                                                    g(Op.OUTPUT, c0=1, c1=1)]
        node = Node(1, graph, seed=7, max_genes=25)
        node.run([])
        self.assertEqual(len(node.graph), 25)
        node.run([])
        self.assertEqual(len(node.graph), 25)

    def test_del_slice_is_half_open(self):
        node = self._run_trigger([g(Op.CONST), g(Op.CONST, p0=1),
                                  g(Op.DEL, p0=-1, p1=1), g(Op.OUTPUT, c0=1, c1=1)])
        # 删除区间 [2-1, 2-1+1) = [1, 2)，即删掉位置 1 的那个 CONST。
        self.assertEqual(len(node.graph), 3)
        self.assertEqual([gene.p0 for gene in node.graph if gene.op is Op.CONST], [0.0])

    def test_mov_moves_block(self):
        node = self._run_trigger([g(Op.CONST), g(Op.CONST),
                                  g(Op.MOV, p0=0, p1=1, p2=-2), g(Op.OUTPUT, c0=1, c1=1)])
        self.assertEqual(len(node.graph), 4)
        self.assertIs(node.graph[0].op, Op.MOV)

    def test_chc_edits_selected_link(self):
        node = self._run_trigger([g(Op.CONST), g(Op.CHC, p0=-1, p1=1, p2=5),
                                  g(Op.OUTPUT, c0=1, c1=1)])
        self.assertEqual(node.graph[0].c1, 5)
        self.assertEqual(node.graph[0].c0, 1)

    def test_chf_changes_type_freely(self):
        node = self._run_trigger([g(Op.CONST), g(Op.CHF, p0=-1, p1=7),
                                  g(Op.OUTPUT, c0=1, c1=1)])
        self.assertIs(node.graph[0].op, Op.ADD)

    def test_chp_changes_param(self):
        node = self._run_trigger([g(Op.CONST), g(Op.CHP, p0=-1, p1=0, p2=9.5),
                                  g(Op.OUTPUT, c0=1, c1=1)])
        self.assertEqual(node.graph[0].p0, 9.5)

    def test_flr_only_drops_later_requests(self):
        # 先被 OUTPUT 追溯到的 FLR 会作废它之后登记的 CHP。
        node = self._run_trigger([
            g(Op.CONST),
            g(Op.FLR),
            g(Op.CHP, p0=-1, p1=0, p2=9.5),
            g(Op.OUTPUT, c0=2, c1=2),
            g(Op.OUTPUT, c0=2, c1=2),
        ])
        self.assertEqual(node.graph[0].p0, 0.0)
        self.assertEqual([item["op"] for item in node.modification_log], ["FLR"])

    def test_same_seed_same_graph(self):
        graph = [g(Op.CONST), g(Op.ADD, p0=0, p1=3), g(Op.OUTPUT, c0=1, c1=1)]
        first = self._run_trigger(graph)
        second = self._run_trigger(graph)
        self.assertEqual(
            [(gene.op, gene.c0, gene.c1) for gene in first.graph],
            [(gene.op, gene.c0, gene.c1) for gene in second.graph],
        )


class NodePersistenceTest(unittest.TestCase):
    """图结构保存/加载只涉及图本身。"""

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "node.json")
            node = Node(7, [g(Op.CONST, p0=3), g(Op.OUTPUT, c0=1, c1=1)], seed=1)
            node.save(path)
            loaded = Node.load(path, seed=1)
            self.assertEqual(loaded.id, 7)
            self.assertEqual([(gene.op, gene.c0, gene.c1, gene.p0)
                              for gene in loaded.graph],
                             [(gene.op, gene.c0, gene.c1, gene.p0) for gene in node.graph])


class GroupTest(unittest.TestCase):
    """群体层：外层寻址、消息汇总、落盘重载。"""

    def test_route_id_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = nodemgr(dps=4, seed=1, db_path=os.path.join(tmp, "t.db"), save_interval=None)
            try:
                # 目标编码乘 10^dps 后取模：-12.7 -> 7000，10005.9 -> 9000。
                self.assertEqual(mgr.route_id(-12.7), 7000)
                self.assertEqual(mgr.route_id(10005.9), 9000)
            finally:
                mgr.close()

    def test_blocked_range_rejects_routed_input(self):
        """拒收区间内的节点（刺激神经元）不能被别的神经元路由写入，但仍可显式激活。"""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = nodemgr(dps=5, seed=1, db_path=os.path.join(tmp, "t.db"),
                          save_interval=None, blocked_range=(10000, 10000))
            try:
                simple = [g(Op.INP), g(Op.OUTPUT, c0=1, c1=1)]
                mgr.addNode(5, simple)
                mgr.addNode(6, simple)
                # dps=5：0.15 -> 15000（刺激区间，应被丢弃），0.5 -> 50000（正常写入）。
                deliveries = mgr.run_round({5: [0.15], 6: [0.5]})
                self.assertNotIn(15000, deliveries)
                self.assertIn(50000, deliveries)
                # 显式激活刺激神经元仍然有效。
                self.assertEqual(mgr.activate(15000, 1.0), 1)
            finally:
                mgr.close()

    def test_aggregate_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            mgr = nodemgr(dps=4, seed=1, db_path=path, save_interval=None)
            # 用显式最小图，避免随机种子图影响本用例对“同目标消息汇总”的断言。
            simple = [g(Op.INP), g(Op.OUTPUT, c0=1, c1=1)]
            mgr.addNode(5, simple)
            mgr.addNode(6, simple)
            # 0.5 -> 5000，0.50003 -> 5000：两条消息汇总到同一个目标神经元。
            deliveries = mgr.run_round({5: [0.5], 6: [0.50003]})
            self.assertIn(5000, deliveries)
            self.assertEqual(len(deliveries[5000]), 2)
            for node_id in (5, 6, 5000):
                mgr.saveNodeToDatabase(node_id)
            mgr.flush(all_nodes=True)
            mgr.close()

            again = nodemgr(dps=4, seed=1, db_path=path, save_interval=None)
            try:
                self.assertIsNotNone(again.getNodeFromCache(5))
                self.assertIsNotNone(again.getNodeFromCache(6))
                self.assertIsNotNone(again.getNodeFromCache(5000))
            finally:
                again.close()


    def test_global_gene_limit_blocks_add(self):
        """全局基因总数上限（默认 800 万，这里用 25 测边界）：ADD 不能把总基因数顶破。"""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = nodemgr(dps=4, seed=1, db_path=os.path.join(tmp, "t.db"),
                          save_interval=None, max_total_genes=25)
            try:
                graph = ([g(Op.CONST) for _ in range(19)]
                         + [g(Op.CONST, p0=math.pi / 2), g(Op.ADD, p0=0),
                            g(Op.OUTPUT, c0=1, c1=1)])
                mgr.addNode(9, graph)
                for _ in range(3):
                    mgr.activate(9, 0.0)
                    mgr.pump_events()
                    genes = (len(mgr._get_genome_native(9)) if mgr.native
                             else len(mgr.getNodeFromCache(9).graph))
                    # 22 个基因 + 全局剩余 3 = 25；后续轮的 ADD 全部被上限拦下。
                    self.assertEqual(genes, 25)
            finally:
                mgr.close()



    def test_activity_statistics_only_count_expanded(self):
        """控制台统计：保留区间内的节点不算扩展神经元（执行/待执行各一套计数）。"""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = nodemgr(dps=5, seed=1, db_path=os.path.join(tmp, "t.db"),
                          save_interval=None, reserved_ranges=[(0, 20000)])
            try:
                graph = [g(Op.CONST, p0=2.5), g(Op.OUTPUT, c0=1, c1=1)]
                mgr.addNode(9, graph)
                mgr.activate(9, 0.5)
                mgr.pump_events()
                summary = mgr.summary()
                # 节点 9 在保留区间内：执行总数 1，扩展 0。
                self.assertEqual(summary["run_total"], 1)
                self.assertEqual(summary["run_expanded"], 0)
                # 目标编码 2.5 -> 2.5*10^5 % 10^5 = 50000，属于扩展神经元。
                self.assertEqual(summary["pending_total"], 1)
                self.assertEqual(summary["pending_expanded"], 1)
            finally:
                mgr.close()


class NativeParityTest(unittest.TestCase):
    """C 版与纯 Python 版在同一张显式图上的行为必须一致（没有 C 库时跳过）。"""

    def setUp(self):
        if load_smcgp_library() is None:
            self.skipTest("未编译 c/libsmcgp_node.so（C 版不可用）")

    def _run_native(self, graph, inputs, dps=4):
        """在原生模式的群体里跑一轮，返回消息汇总与回写后的图。"""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = nodemgr(dps=dps, seed=1, db_path=os.path.join(tmp, "t.db"), save_interval=None)
            try:
                self.assertTrue(mgr.native)
                mgr.addNode(9, graph, seed=1)
                deliveries = mgr.run_round({9: list(inputs)})
                return deliveries, mgr._get_genome_native(9)
            finally:
                mgr.close()

    def test_parity_on_emission(self):
        graph = [g(Op.CONST, p0=2), g(Op.CONST, p0=3), g(Op.DADD, c0=2, c1=1),
                 g(Op.OUTPUT, c0=1, c1=1)]
        python_emissions = Node(9, graph, seed=1).run([0.5])
        deliveries, native_graph = self._run_native(graph, [0.5])
        self.assertEqual([(item.value, item.destination) for item in python_emissions],
                         [(5.0, 5.0)])
        # 目标编码 5.0 -> 5.0*10^4 % 10^4 = 0。
        self.assertEqual(deliveries, {0: [5.0]})
        self.assertEqual([(gene.op, gene.c0, gene.c1) for gene in native_graph],
                         [(gene.op, gene.c0, gene.c1) for gene in graph])

    def test_parity_on_del_boundary(self):
        graph = [g(Op.CONST), g(Op.CONST, p0=1), g(Op.DEL, p0=-1, p1=1),
                 g(Op.OUTPUT, c0=1, c1=1)]
        python_node = Node(9, graph, seed=1)
        python_node.run([0.0])
        _, native_graph = self._run_native(graph, [0.0])
        self.assertEqual(len(native_graph), len(python_node.graph))
        self.assertEqual([gene.op for gene in native_graph],
                         [gene.op for gene in python_node.graph])

    def test_parity_on_add_count(self):
        """触发值 a=π/2 时两版都插入 16 个节点（16·sin(π/2)）。"""
        graph = [g(Op.CONST, p0=math.pi / 2), g(Op.ADD, p0=0),
                 g(Op.OUTPUT, c0=1, c1=1)]
        python_node = Node(9, graph, seed=1)
        python_node.run([0.0])
        _, native_graph = self._run_native(graph, [0.0])
        self.assertEqual(len(python_node.graph), 19)
        self.assertEqual(len(native_graph), 19)

    def test_native_add_stops_at_max_genes(self):
        """C 引擎的 ADD 数量映射与基因上限都与 Python 版一致（dps=1 -> 2.5 × 10 = 25）。"""
        graph = [g(Op.CONST) for _ in range(19)] + [g(Op.CONST, p0=math.pi / 2),
                                                    g(Op.ADD, p0=0),
                                                    g(Op.OUTPUT, c0=1, c1=1)]
        python_node = Node(9, graph, seed=1, max_genes=25)
        python_node.run([0.0])
        _, native_graph = self._run_native(graph, [0.0], dps=1)
        self.assertEqual(len(python_node.graph), 25)
        self.assertEqual(len(native_graph), 25)


    def test_rounds_keep_running(self):
        """连续多轮都必须真的执行到节点（防止待执行标记泄漏导致网络静默）。"""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = nodemgr(dps=4, seed=1, db_path=os.path.join(tmp, "t.db"), save_interval=None)
            try:
                self.assertTrue(mgr.native)
                mgr.addNode(5, [g(Op.INP), g(Op.OUTPUT, c0=1, c1=1)])
                fired = []
                for _ in range(3):
                    mgr.activate(5, 0.5)
                    fired.append(mgr.pump_events())
                self.assertTrue(all(count > 0 for count in fired), fired)
            finally:
                mgr.close()


if __name__ == "__main__":
    unittest.main()
