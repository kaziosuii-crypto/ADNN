import os
import sqlite3
import tempfile
import time

from compute.mp_manager import MPNodeManager


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "nodes.db")
        manager = MPNodeManager(0, 0.2, 4, 0.1, path, worker_count=2)
        callbacks = []
        manager.register_callback(0, lambda node: callbacks.append(node.id))
        manager.addNode(1, 0.5)
        manager.activate(0, 1)
        manager.activate(1, 1)
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                manager.pump_events()
                if callbacks and manager.graph_payload()["edges"]:
                    break
                time.sleep(0.02)
            assert callbacks and manager.graph_payload()["edges"]
            assert len({process.pid for process in manager._processes}) == 2
            # 批量激活后应产生一帧增量图，取走后再取应为空。
            manager.activate_many([(0, 1), (1, 1)])
            time.sleep(0.1)
            manager.pump_events()
            patch = manager.graph_patch_json()
            assert patch and '"nodes"' in patch, patch
            assert manager.graph_patch_json() is None
            summary = manager.summary()
            assert summary["nodes"] >= 2 and summary["edges"] >= 2, summary
        finally:
            manager.close()
        database = sqlite3.connect(path)
        try:
            assert database.execute("SELECT count(*) FROM nodes WHERE forwards != '{}' ").fetchone()[0] >= 2
        finally:
            database.close()
        print("spawn smoke test passed")


if __name__ == "__main__":
    main()
