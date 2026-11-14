import random
import threading

from demo.pong.pong import COLS, ROWS, TICK_MS, Pong, PongWindow
from compute.mp_manager import MPNodeManager

ID_UP        = ROWS * COLS       # 800
ID_DOWN      = ROWS * COLS + 1   # 801
ID_SENSE_UP  = ROWS * COLS + 2   # 802
ID_SENSE_DOWN = ROWS * COLS + 3  # 803
DPS = 4
TIME_WINDOW = 2.0  # seconds (2000 ms)
SAVE_INTERVAL = 2.0  # None: save only when stopping the network
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
        """Reserve IDs without reassigning persisted nodes or their existing targets."""
        size = ROWS * COLS
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
            groups = {name: [start] for name, start in GROUP_STARTS.items()}
            for name, position, node_id in existing:
                if name not in groups or position != len(groups[name]):
                    raise ValueError("Invalid persisted Pong group mapping")
                groups[name].append(node_id)
            used = set(manager.cacheNodes) | set(range(size + 4))
            for node in manager.cacheNodes.values():
                used.update(node.forwards)
            for ids in groups.values():
                used.update(ids)
            candidates = (node_id for node_id in range(size + 4, 10 ** DPS + 1)
                          if node_id not in used)
            additions = []
            for name, ids in groups.items():
                if len(ids) > size:
                    raise ValueError("Persisted Pong group is larger than the visual group")
                while len(ids) < size:
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
        return groups

    def _on_up(self, node):
        self.game.move_right_up(1)

    def _on_down(self, node):
        self.game.move_right_down(1)

    def _activate(self, node_id, value):
        if not self.manager.stopped:
            self.manager.activate(node_id, value)

    def on_tick(self, game):
        self.manager.pump_events()
        if self.manager.stopped:
            return
        grid = game.get_grid()
        for row in range(ROWS):
            for col in range(COLS):
                self._activate(row * COLS + col, grid[row][col])
        for direction, count in (("sense_up", game.paddle_moved_up),
                                 ("sense_down", game.paddle_moved_down)):
            for node_id in random.sample(self.groups[direction], min(count, len(self.groups[direction]))):
                self._activate(node_id, 1)
        if game.ball_out:
            self.manager.add_dopamine(1)
            game.ball_out = False
        if game.paddle_hit:
            self.manager.dopamine = 0
            game.paddle_hit = False
        self.game_state = self._game_snapshot()

    def _game_snapshot(self):
        game = self.game
        return {
            "grid": game.get_grid(), "rows": ROWS, "cols": COLS,
            "score_left": game.score_left, "score_right": game.score_right,
            "up": game.commanded_up, "down": game.commanded_down,
            "moved_up": game.paddle_moved_up, "moved_down": game.paddle_moved_down,
        }

    def start(self):
        def loop():
            while not self._stop_tick.wait(TICK_MS / 1000):
                if self.manager.stopped:
                    break
                self.game.update()
                self.on_tick(self.game)
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
