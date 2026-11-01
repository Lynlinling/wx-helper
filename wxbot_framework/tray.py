"""Windows 通知区域托盘控制器。"""
from __future__ import annotations

import logging
from PIL import Image, ImageDraw

try:
    import pystray
except ImportError:
    pystray = None

LOGGER = logging.getLogger("Tray")


def make_icon(size=64):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 64
    def box(values):
        return tuple(round(value * scale) for value in values)
    draw.ellipse(box((7, 16, 54, 49)), fill="#6ac5df", outline="#1d7392", width=max(1, round(2 * scale)))
    draw.polygon([tuple(round(v * scale) for v in point) for point in ((48, 30), (61, 19), (57, 34), (62, 45), (47, 39))], fill="#55b5d1", outline="#1d7392")
    draw.ellipse(box((20, 25, 26, 31)), fill="#ffffff", outline="#1d7392")
    draw.ellipse(box((22, 27, 25, 30)), fill="#173047")
    draw.arc(box((28, 29, 42, 41)), 10, 160, fill="#1d6079", width=max(1, round(2 * scale)))
    draw.ellipse(box((31, 8, 36, 20)), fill="#55b5d1", outline="#1d7392")
    return image


class TrayController:
    def __init__(self, panel):
        self.panel = panel
        self.icon = None
        self.available = pystray is not None

    def start(self):
        if not self.available or self.icon is not None:
            return False
        menu = pystray.Menu(
            pystray.MenuItem("打开控制台", self._open),
            pystray.MenuItem("显示/隐藏桌宠", self._toggle_pet),
            pystray.MenuItem("启动机器人", self._start_bot),
            pystray.MenuItem("停止机器人", self._stop_bot),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("完全退出", self._exit),
        )
        self.icon = pystray.Icon("wx-helper", make_icon(), "wx-helper · 鲸鱼娘", menu)
        try:
            self.icon.run_detached()
        except Exception:
            self.icon = None
            raise
        return True

    def _call_ui(self, callback):
        try:
            self.panel.window.after(0, callback)
        except Exception:
            LOGGER.debug("托盘回调失败", exc_info=True)

    def _open(self, _icon=None, _item=None):
        self._call_ui(self.panel.show_window)

    def _toggle_pet(self, _icon=None, _item=None):
        self._call_ui(self.panel.toggle_pet)

    def _start_bot(self, _icon=None, _item=None):
        self._call_ui(lambda: self._run_panel_action(self.panel._start_bot))

    def _stop_bot(self, _icon=None, _item=None):
        self._call_ui(lambda: self._run_panel_action(self.panel._stop_bot))

    def _run_panel_action(self, callback):
        self.panel.show_window()
        callback()

    def _exit(self, _icon=None, _item=None):
        self._call_ui(self.panel.full_exit)

    def stop(self):
        icon, self.icon = self.icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                LOGGER.debug("托盘停止失败", exc_info=True)
