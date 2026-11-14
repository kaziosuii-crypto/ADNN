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

GRAPH_INTERVAL = 0.25  # 图结构更新频率：每秒 4 帧增量，状态变化更实时
# 每积累这么多增量帧就补发一帧全量，防止客户端结构长期偏离。
FULL_GRAPH_EVERY = 64
# 状态帧最小间隔：自适应帧率可能远高于 20fps，这里节流避免淹没 stdout 与浏览器。
STATE_MIN_INTERVAL = 0.04
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
    with _emit_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def net_summary(manager):
    return manager.summary()


def graph_line(manager):
    # graph_json() 已在 2 个核上完成序列化，这里只拼上消息类型，避免父进程再 dumps 一次。
    # pump=False：图线程不等待事件追平，避免和图/状态推送互相阻塞。
    return '{"type":"graph",' + manager.graph_json(pump=False)[1:]


def graph_loop(manager, shutdown, force_full):
    """独立线程负责图结构推送：按固定间隔发全量帧或增量帧。

    全量帧在启动时先发一次；之后只发 patch，客户端请求或累计过多增量时再补全量。
    这样状态推送（跟随自适应帧率）不再被整张图的序列化挡住。
    """
    next_at = time.monotonic() + GRAPH_INTERVAL
    full = True
    since_full = 0
    frames = 0
    _debug("图线程启动")
    while not shutdown.is_set():
        delay = next_at - time.monotonic()
        if delay > 0 and shutdown.wait(delay):
            break
        next_at += GRAPH_INTERVAL
        if force_full.is_set() or full or since_full >= FULL_GRAPH_EVERY:
            force_full.clear()
            started = time.monotonic()
            emit_line(graph_line(manager))
            full = False
            since_full = 0
            frames += 1
            _debug("发出全量图帧", frames, "耗时",
                   round((time.monotonic() - started) * 1000, 2), "ms")
        else:
            patch = manager.graph_patch_json()
            if patch:
                emit_line('{"type":"patch",' + patch[1:])
                since_full += 1
                frames += 1
                _debug("发出增量图帧", frames, "距上次全量", since_full, "帧")
    _debug("图线程退出，共发送", frames, "帧")


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
    force_full = threading.Event()
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
        """读取父进程通过 stdin 下发的命令：stop 结束，graph 立即补一帧全量。"""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            command = message.get("cmd")
            if command == "stop":
                _debug("收到 stop 命令")
                break
            if command == "graph":
                _debug("收到 graph 命令，请求全量帧")
                force_full.set()
        # 收到停止命令，或父进程关闭了 stdin，都走同一条结束流程。
        shutdown.set()

    runtime.start()
    threading.Thread(target=read_commands, name="bridge-stdin", daemon=True).start()
    graph_thread = threading.Thread(target=graph_loop, args=(runtime.manager, shutdown, force_full),
                                    name="bridge-graph", daemon=True)
    graph_thread.start()

    # 主线程不再自己走 50ms 定时器，只等停止命令。
    shutdown.wait()
    shutdown.set()
    graph_thread.join()
    runtime.close()
    emit({"type": "stopped", "net": net_summary(runtime.manager)})
    _debug("共发出状态帧", state_frames)
    emit({"type": "closed"})


if __name__ == "__main__":
    main()
