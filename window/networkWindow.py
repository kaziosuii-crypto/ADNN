import queue
import threading
import tkinter as tk


class NetworkControlWindow:
    def __init__(self, master, manager, on_stopped=None):
        self.manager = manager
        self.on_stopped = on_stopped
        self.results = queue.Queue()
        self.root = tk.Toplevel(master)
        self.root.title("网络控制")
        self.root.geometry("340x110")
        self.button = tk.Button(
            self.root, text="强制结束网络并落盘",
            command=self.stop, font=("", 14), bg="#b91c1c", fg="white",
        )
        self.button.pack(fill="both", expand=True, padx=12, pady=12)
        self.root.after(100, self._poll)

    def stop(self):
        self.manager.request_stop()
        self.button.configure(state="disabled", text="正在结束窗口并落盘…")
        # Joining activation threads and committing must not block Tk's event loop.
        threading.Thread(target=self._save, daemon=True).start()

    def _save(self):
        try:
            self.manager.close()
            self.results.put(None)
        except Exception as error:
            self.results.put(str(error))

    def _poll(self):
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            if result is None:
                self.button.configure(text="网络已结束，已落盘", bg="#166534")
                if self.on_stopped:
                    self.on_stopped()
            else:
                self.button.configure(text=f"保存失败，点击重试\n{result}", state="normal")
        self.root.after(100, self._poll)


class NetworkStateWindow:
    ACTIVE = "#16a34a"
    INACTIVE = "#dc2626"
    CELL = 20
    COLUMNS = 40

    def __init__(self, master, manager, special_nodes):
        self.manager = manager
        self.special_nodes = dict(special_nodes)
        self.root = tk.Toplevel(master)
        self.root.title("全网络实时状态")
        self.root.geometry("940x800")
        self.summary = tk.Label(self.root, anchor="w", font=("", 12))
        self.summary.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(
            self.root, anchor="w",
            text="绿：激活窗口内  |  红：未激活  |  金色边框：adapter 的 804 个节点\n"
                 "每 200ms 读取一次状态；点击节点查看数值和连接；下方可滚动查看全部节点。",
        ).pack(fill="x", padx=12)

        area = tk.Frame(self.root)
        area.pack(fill="both", expand=True, padx=12, pady=8)
        self.canvas = tk.Canvas(area, bg="#111827", highlightthickness=0)
        vertical = tk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        horizontal = tk.Scrollbar(area, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        self.canvas.bind("<Button-1>", self._select)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-int(e.delta / 120), "units"))

        detail_area = tk.Frame(self.root)
        detail_area.pack(fill="x", padx=12, pady=(0, 10))
        self.detail = tk.Text(detail_area, height=6, wrap="word", state="disabled")
        detail_scroll = tk.Scrollbar(detail_area, command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side="left", fill="x", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self.items = {}
        self.colors = {}
        self.extra_ids = []
        self.selected = 0
        self._detail_text = None
        self.canvas.create_text(12, 14, anchor="nw", fill="white", text="视觉输入：ID 0–799，20 行 × 40 列")
        self.canvas.create_text(12, 452, anchor="nw", fill="white", text="操作和感觉：ID 800–803")
        self.canvas.create_text(12, 544, anchor="nw", fill="white", text="其余网络节点（包括从数据库恢复的节点）")
        self._refresh()

    def _add_node(self, id):
        tag = f"node:{id}"
        if 0 <= id < 800:
            x, y = 12 + (id % 40) * self.CELL, 42 + (id // 40) * self.CELL
            width, height = self.CELL - 2, self.CELL - 2
        elif 800 <= id < 804:
            x, y = 12 + (id - 800) * 200, 484
            width, height = 190, 42
        else:
            index = len(self.extra_ids)
            self.extra_ids.append(id)
            x, y = 12 + (index % self.COLUMNS) * self.CELL, 574 + (index // self.COLUMNS) * self.CELL
            width, height = self.CELL - 2, self.CELL - 2
        self.items[id] = self.canvas.create_rectangle(
            x, y, x + width, y + height, fill=self.INACTIVE,
            outline="#facc15" if id in self.special_nodes else "#475569",
            width=2 if id in self.special_nodes else 1, tags=(tag,),
        )
        if 800 <= id < 804:
            self.canvas.create_text(
                x + width / 2, y + height / 2, text=f"{id}  {self.special_nodes[id]}",
                fill="white", tags=(tag,),
            )

    def _select(self, event):
        items = self.canvas.find_withtag("current")
        for item in items:
            for tag in self.canvas.gettags(item):
                if tag.startswith("node:"):
                    self.selected = int(tag.split(":")[1])
                    self._show_detail()
                    return

    def _show_detail(self):
        node = self.manager.getNodeFromCache(self.selected)
        if node is None:
            return
        # This is observational, not a barrier across the network.
        forwards = node.forwards.copy()
        role = self.special_nodes.get(node.id, "普通网络节点")
        text = (
            f"ID {node.id} | {role} | {'激活' if node.active else '未激活'}\n"
            f"value={float(node.value):.10g} | 窗口={node.timeWindow}s | dps={node.dps}\n"
            f"前向连接 {len(forwards)} 条：\n"
            + "\n".join(f"{target}  权重={weight:.6g}" for target, weight in sorted(forwards.items()))
        )
        if text != self._detail_text:
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0", text)
            self.detail.configure(state="disabled")
            self._detail_text = text

    def _refresh(self):
        snapshot = self.manager.snapshot()
        nodes = snapshot["nodes"]
        active = sum(state for _, state, _, _ in nodes)
        edges = sum(count for _, _, _, count in nodes)
        status = "已停止并落盘" if snapshot["saved"] else ("正在停止" if snapshot["stopped"] else "运行中")
        self.summary.configure(
            text=f"{status} | 节点 {len(nodes)} | 激活 {active} | 连接 {edges} | dopamine {snapshot['dopamine']:.3g}"
                 + (f" | 保存异常：{snapshot['save_error']}" if snapshot["save_error"] else "")
        )
        added = False
        for id, state, _, _ in nodes:
            if id not in self.items:
                self._add_node(id)
                added = True
            color = self.ACTIVE if state else self.INACTIVE
            if self.colors.get(id) != color:
                self.canvas.itemconfigure(self.items[id], fill=color)
                self.colors[id] = color
        if added:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._show_detail()
        self.root.after(200, self._refresh)
