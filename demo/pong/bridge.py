"""把 Pong 神经网络的状态以 JSON 行输出到 stdout，供 Node.js 网页渲染。

网络本身保持原有时序：每个神经元仍有独立的激活窗口，游戏节拍不变。
本进程只做观察和转发，不参与激活计算。
"""

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from demo.pong.pong import COLS, ROWS
from demo.pong.pong_adapter import PongRuntime

STATE_INTERVAL = 0.2   # 与游戏节拍一致
GRAPH_INTERVAL = 0.8   # 图结构更新频率
DB_PATH = os.environ.get("BTB_DB", "nodes.db")


_emit_lock = threading.Lock()


def emit(payload):
    line = json.dumps(payload, separators=(",", ":"))
    with _emit_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def net_summary(manager):
    return manager.summary()


def graph_payload(manager):
    return manager.graph_payload()


def main():
    runtime = PongRuntime(db_path=DB_PATH)
    emit({
        "type": "meta",
        "rows": ROWS,
        "cols": COLS,
        "groups": runtime.groups,
        "visual_size": ROWS * COLS,
    })

    shutdown = threading.Event()

    def read_commands():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("cmd") == "stop":
                break
        # 收到停止命令，或父进程关闭了 stdin，都走同一条结束流程。
        shutdown.set()

    runtime.start()
    threading.Thread(target=read_commands, daemon=True).start()

    counter = 0
    while not shutdown.wait(STATE_INTERVAL):
        counter += 1
        emit({"type": "state", "game": runtime.game_state, "net": net_summary(runtime.manager)})
        if counter % int(GRAPH_INTERVAL / STATE_INTERVAL) == 0:
            emit({"type": "graph", **graph_payload(runtime.manager)})

    runtime.close()
    emit({"type": "stopped", "net": net_summary(runtime.manager)})
    emit({"type": "closed"})


if __name__ == "__main__":
    main()
