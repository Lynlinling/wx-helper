"""轻量的鲸鱼娘桌宠，不读取屏幕、不模拟鼠标。"""
from __future__ import annotations

import tkinter as tk


class WhalePet:
    def __init__(self, master, on_open=None, on_exit=None):
        self.on_open = on_open
        self.on_exit = on_exit
        self.window = tk.Toplevel(master)
        self.window.title("DeepSeek 鲸鱼娘")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        try:
            self.window.attributes("-alpha", 0.96)
            self.window.wm_attributes("-transparentcolor", "#000001")
        except tk.TclError:
            pass
        self.window.configure(bg="#000001")
        self.canvas = tk.Canvas(self.window, width=190, height=170, bg="#000001",
                                highlightthickness=0)
        self.canvas.pack()
        self.status = "待命中"
        self._offset = (0, 0)
        self._draw()
        for widget in (self.window, self.canvas):
            widget.bind("<ButtonPress-1>", self._begin_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<Double-Button-1>", lambda _event: self.hide())
            widget.bind("<Button-3>", self._show_menu)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self._place_default()

    def _place_default(self):
        width, height = self.window.winfo_screenwidth(), self.window.winfo_screenheight()
        self.window.geometry(f"190x170+{max(0, width - 240)}+{max(0, height - 260)}")

    def _draw(self):
        c = self.canvas
        c.delete("all")
        c.create_oval(24, 128, 166, 157, fill="#6b7a91", outline="")
        c.create_oval(24, 42, 164, 136, fill="#6ac5df", outline="#2486a5", width=2)
        c.create_polygon(153, 90, 183, 70, 174, 101, 187, 123, 151, 111,
                         fill="#55b5d1", outline="#2486a5")
        c.create_oval(64, 71, 77, 86, fill="white", outline="#2486a5")
        c.create_oval(69, 75, 74, 82, fill="#27364d", outline="")
        c.create_oval(80, 91, 89, 96, fill="#f5a8b8", outline="")
        c.create_polygon(44, 50, 35, 41, 47, 43, 53, 34, 59, 44, 71, 40,
                         64, 51, 54, 54, fill="#ee8fa8", outline="#bf5875")
        c.create_arc(92, 83, 125, 111, start=195, extent=150, style="arc",
                     outline="#1e6079", width=2)
        c.create_oval(100, 31, 112, 50, fill="#55b5d1", outline="#2486a5")
        c.create_text(95, 12, text="鲸鱼上岸", fill="#2486a5", font=("Microsoft YaHei UI", 9))
        c.create_rectangle(17, 145, 173, 166, fill="#173047", outline="#173047")
        c.create_text(95, 155, text=f"DeepSeek鲸鱼娘·{self.status}", fill="#dff8ff",
                      font=("Microsoft YaHei UI", 9))

    def _begin_drag(self, event):
        self._offset = (event.x_root - self.window.winfo_x(), event.y_root - self.window.winfo_y())

    def _drag(self, event):
        x = event.x_root - self._offset[0]
        y = event.y_root - self._offset[1]
        self.window.geometry(f"+{x}+{y}")

    def set_status(self, text):
        status = str(text).strip()[:18] or "待命中"
        if status == self.status:
            return
        self.status = status
        if self.window.winfo_exists():
            self._draw()

    def show(self):
        self.window.deiconify()
        self.window.lift()

    def hide(self):
        self.window.withdraw()

    def destroy(self):
        if self.window.winfo_exists():
            self.window.destroy()

    def _show_menu(self, event):
        menu = tk.Menu(self.window, tearoff=False)
        if self.on_open is not None:
            menu.add_command(label="打开控制台", command=self.on_open)
        menu.add_command(label="隐藏桌宠", command=self.hide)
        if self.on_exit is not None:
            menu.add_separator()
            menu.add_command(label="完全退出", command=self.on_exit)
        menu.tk_popup(event.x_root, event.y_root)


def create_pet(master, on_open=None, on_exit=None):
    try:
        return WhalePet(master, on_open=on_open, on_exit=on_exit)
    except (tk.TclError, RuntimeError):
        return None
