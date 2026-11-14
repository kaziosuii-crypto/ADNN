import tkinter as tk
import queue


class StatusWindow:
    def __init__(self, title="Status", initcolor="red", master=None):
        self.comm = queue.Queue()
        self.root = None
        self.master = master
        self.title = title
        self.initcolor = initcolor

    def open(self):
        self.root = tk.Toplevel(self.master) if self.master is not None else tk.Tk()
        self.root.title(self.title)
        self.root.geometry("200x200")
        self.root.configure(bg=self.initcolor)

        def destroyed(event):
            if event.widget is self.root:
                self.root = None

        def update():
            while not self.comm.empty():
                msg = self.comm.get_nowait()
                self.root.configure(bg=msg)
            self.root.after(20, update)

        self.root.bind("<Destroy>", destroyed)
        self.root.after(20, update)

    def close(self):
        if self.root:
            self.root.destroy()

    def set_color(self, color):
        if self.root:
            self.comm.put_nowait(color)
