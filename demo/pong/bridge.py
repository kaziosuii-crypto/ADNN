"""把 Pong 神经网络的状态以 JSON 行输出到 stdout，供 Node.js 网页渲染。

网络本身保持原有时序：每个神经元仍有独立的激活窗口，游戏节拍不变。
本进程只做观察和转发，不参与激活计算。
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from demo.pong.pong import COLS, ROWS
from demo.pong.pong_adapter import PongRuntime

# 控制台已不再显示拓扑图：bridge 只推送状态帧（Pong、得分、总体统计、帧率）。
# 状态帧最小间隔：最高 60Hz，保证页面跟得上游戏节奏。
STATE_MIN_INTERVAL = 1.0 / 60.0
DB_PATH = os.environ.get("BTB_DB", "nodes.db")

# 大量排查输出：设置 BTB_DEBUG=1 后可在 stderr 看到图线程与命令细节。
DEBUG = os.environ.get("BTB_DEBUG") == "1"


def _debug(*args):
    """调试输出走 stderr，不会污染 stdout 的 JSON 数据流。"""
    if DEBUG:
        print("[bridge]", *args, file=sys.stderr, flush=True)


_emit_lock = threading.Lock()


def emit(payload):
    line = json.dumps(payload, separators=(",", ":"))
    with _emit_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def emit_line(line):
    """原样输出一行 JSON（保留给未来需要直接拼装的帧类型）。"""
    with _emit_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def net_summary(manager):
    return manager.summary()


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
    state_frames = 0
    last_state_at = 0.0

    def emit_state(game_state):
        """状态帧由节拍线程直接调用：跟随自适应帧率，但按最小间隔节流。"""
        nonlocal state_frames, last_state_at
        now = time.monotonic()
        if now - last_state_at < STATE_MIN_INTERVAL:
            return
        last_state_at = now
        state_frames += 1
        summary = net_summary(runtime.manager)
        emit({"type": "state", "game": game_state, "net": summary,
              "fps": round(runtime.fps, 2)})
        if DEBUG and state_frames % 20 == 0:
            _debug("状态帧", state_frames, "帧率", round(runtime.fps, 2),
                   "节点", summary["nodes"], "激活", summary["active"], "连接", summary["edges"])

    # 把 state 推送绑到游戏节拍上；bridge 主线程只负责命令与收尾。
    runtime.on_state = emit_state

    def read_commands():
        """读取父进程通过 stdin 下发的命令：只处理 stop（控制台已无拓扑图）。"""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("cmd") == "stop":
                _debug("收到 stop 命令")
                break
        # 收到停止命令，或父进程关闭了 stdin，都走同一条结束流程。
        shutdown.set()

    runtime.start()
    threading.Thread(target=read_commands, name="bridge-stdin", daemon=True).start()

    # 主线程只等停止命令；状态帧由节拍线程负责推送。
    shutdown.wait()
    shutdown.set()
    runtime.close()
    emit({"type": "stopped", "net": net_summary(runtime.manager)})
    _debug("共发出状态帧", state_frames)
    emit({"type": "closed"})


if __name__ == "__main__":
    main()
