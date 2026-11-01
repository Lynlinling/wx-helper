from __future__ import annotations

from wxbot_framework.tray import TrayController, make_icon


def test_tray_icon_is_generated_without_external_asset():
    image = make_icon(64)
    assert image.size == (64, 64)
    assert image.mode == "RGBA"
    assert image.getbbox() is not None


def test_tray_menu_callbacks_are_dispatched_to_window_thread():
    scheduled = []
    class Window:
        def after(self, delay, callback):
            scheduled.append((delay, callback))
    events = []
    class Panel:
        window = Window()
        def show_window(self): events.append("open")
        def toggle_pet(self): events.append("pet")
        def _start_bot(self): events.append("start")
        def _stop_bot(self): events.append("stop")
        def full_exit(self): events.append("exit")
    tray = TrayController(Panel())
    tray._open()
    tray._toggle_pet()
    tray._start_bot()
    tray._stop_bot()
    tray._exit()
    for delay, callback in scheduled:
        assert delay == 0
        callback()
    assert events == ["open", "pet", "open", "start",
                     "open", "stop", "exit"]
