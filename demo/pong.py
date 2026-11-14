import random
import tkinter as tk

CELL_SIZE = 20
COLS = 40
ROWS = 20
PADDLE_HEIGHT = 3
TICK_MS = 80


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
        self._right_command = 0  # -1 上, 0 不动, 1 下

    def move_right_up(self, val):
        """外部接口：val=1 向上一丢丢，0 不动"""
        if val == 1:
            self._right_command = -1

    def move_right_down(self, val):
        """外部接口：val=1 向下一丢丢，0 不动"""
        if val == 1:
            self._right_command = 1

    def update(self):
        self.ball_x += self.dx
        self.ball_y += self.dy

        if self.ball_y <= 0 or self.ball_y >= ROWS - 1:
            self.dy = -self.dy

        if self.ball_x <= 1 and self.left_paddle_y <= self.ball_y < self.left_paddle_y + PADDLE_HEIGHT:
            self.dx = -self.dx
        if self.ball_x >= COLS - 2 and self.right_paddle_y <= self.ball_y < self.right_paddle_y + PADDLE_HEIGHT:
            self.dx = -self.dx

        if self.ball_x < 0:
            self.score_right += 1
            self._reset()
        elif self.ball_x >= COLS:
            self.score_left += 1
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
        if self._right_command == 1:
            self.right_paddle_y = min(self.right_paddle_y + 1, ROWS - PADDLE_HEIGHT)
        elif self._right_command == -1:
            self.right_paddle_y = max(self.right_paddle_y - 1, 0)
        self._right_command = 0

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
    def __init__(self):
        self.game = Pong()
        self.root = tk.Tk()
        self.root.title("Pong 20x40  ↑/↓ 控制右桨")
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
        grid = self.game.get_grid()
        for y in range(ROWS):
            for x in range(COLS):
                color = "white" if grid[y][x] else "black"
                self.canvas.itemconfig(self.rects[(x, y)], fill=color)
        self.root.after(TICK_MS, self._tick)


if __name__ == "__main__":
    PongWindow()
