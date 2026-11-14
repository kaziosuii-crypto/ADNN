import random
import threading
import tkinter as tk

# 40 横向 × 100 纵向像素：视觉神经元 = 40*100 = 4000 个。
CELL_SIZE = 8  # 画面 320×800，避免窗口过高
COLS = 40
ROWS = 100
PADDLE_HEIGHT = 3
# 初始帧长；PongRuntime 会按“最慢单个神经元耗时”每 3 分钟自适应调整。
TICK_MS = 50


class Pong:
    def __init__(self):
        self.ball_x = COLS // 2
        self.ball_y = ROWS // 2
        self.dx = 1
        self.dy = 1
        self.left_paddle_y = ROWS // 2 - 1
        self.right_paddle_y = ROWS // 2 - 1
        self.score_left = 0
        self.score_right = 0
        self._command_lock = threading.Lock()
        self._right_up = 0
        self._right_down = 0
        self.commanded_up = 0
        self.commanded_down = 0
        self.ball_out = False        # 球出画面时置 True，读取后由外部清除
        # 出界方向：'left' = 球从左边界飞出（左桨漏球，右玩家得分）；
        #           'right' = 球从右边界飞出（右桨漏球，左玩家得分）。
        # adapter 用它区分 dopamine 规则，读取后由外部清除。
        self.out_side = None
        self.paddle_hit = False      # 右桨击中球时置 True，读取后由外部清除
        self.paddle_moved_up = 0
        self.paddle_moved_down = 0

    def move_right_up(self, val):
        if val == 1:
            with self._command_lock:
                self._right_up += 1

    def move_right_down(self, val):
        if val == 1:
            with self._command_lock:
                self._right_down += 1

    def update(self):
        self.ball_x += self.dx
        self.ball_y += self.dy

        if self.ball_y <= 0 or self.ball_y >= ROWS - 1:
            self.dy = -self.dy

        if self.ball_x <= 1 and self.left_paddle_y <= self.ball_y < self.left_paddle_y + PADDLE_HEIGHT:
            self.dx = -self.dx

        if self.ball_x >= COLS - 2 and self.right_paddle_y <= self.ball_y < self.right_paddle_y + PADDLE_HEIGHT:
            self.dx = -self.dx
            self.paddle_hit = True

        if self.ball_x < 0:
            # 球从左边界飞出：左桨漏球，右玩家得分。
            self.score_right += 1
            self.out_side = "left"
            self.ball_out = True
            self._reset()
        elif self.ball_x >= COLS:
            # 球从右边界飞出：右桨漏球，左玩家得分。
            self.score_left += 1
            self.out_side = "right"
            self.ball_out = True
            self._reset()

        self._move_left_ai()
        self._apply_right_command()

    def _reset(self):
        self.ball_x = COLS // 2
        self.ball_y = ROWS // 2
        self.dx = random.choice([1, -1])
        self.dy = random.choice([1, -1])

    def _move_left_ai(self):
        target = self.ball_y - 1
        if self.left_paddle_y < target:
            self.left_paddle_y = min(self.left_paddle_y + 1, ROWS - PADDLE_HEIGHT)
        elif self.left_paddle_y > target:
            self.left_paddle_y = max(self.left_paddle_y - 1, 0)

    def _apply_right_command(self):
        with self._command_lock:
            self.commanded_up, self.commanded_down = self._right_up, self._right_down
            self._right_up = self._right_down = 0
        before = self.right_paddle_y
        self.right_paddle_y = max(
            0, min(ROWS - PADDLE_HEIGHT, before + self.commanded_down - self.commanded_up)
        )
        self.paddle_moved_up = max(0, before - self.right_paddle_y)
        self.paddle_moved_down = max(0, self.right_paddle_y - before)

    def get_grid(self):
        grid = [[0] * COLS for _ in range(ROWS)]
        for i in range(PADDLE_HEIGHT):
            if 0 <= self.left_paddle_y + i < ROWS:
                grid[self.left_paddle_y + i][0] = 1
            if 0 <= self.right_paddle_y + i < ROWS:
                grid[self.right_paddle_y + i][COLS - 1] = 1
        if 0 <= self.ball_y < ROWS and 0 <= self.ball_x < COLS:
            grid[self.ball_y][self.ball_x] = 1
        return grid


class PongWindow:
    def __init__(self, game=None, on_tick=None, root=None):
        self.game = game if game is not None else Pong()
        self.on_tick = on_tick
        self.root = root if root is not None else tk.Tk()
        self.root.title(f"Pong {ROWS}x{COLS}  ↑/↓ 控制右桨")
        self.canvas = tk.Canvas(
            self.root, width=COLS * CELL_SIZE, height=ROWS * CELL_SIZE, bg="black"
        )
        self.canvas.pack()
        self.rects = {}
        for y in range(ROWS):
            for x in range(COLS):
                self.rects[(x, y)] = self.canvas.create_rectangle(
                    x * CELL_SIZE, y * CELL_SIZE,
                    (x + 1) * CELL_SIZE, (y + 1) * CELL_SIZE,
                    fill="black", outline="",
                )
        self.root.bind("<Up>", lambda e: self.game.move_right_up(1))
        self.root.bind("<Down>", lambda e: self.game.move_right_down(1))
        self.root.after(TICK_MS, self._tick)
        self.root.mainloop()

    def _tick(self):
        self.game.update()
        if self.on_tick is not None:
            self.on_tick(self.game)
        grid = self.game.get_grid()
        for y in range(ROWS):
            for x in range(COLS):
                color = "white" if grid[y][x] else "black"
                self.canvas.itemconfig(self.rects[(x, y)], fill=color)
        self.root.after(TICK_MS, self._tick)


if __name__ == "__main__":
    PongWindow()
