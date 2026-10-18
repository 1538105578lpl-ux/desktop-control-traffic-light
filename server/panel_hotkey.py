"""Temporary global pause shortcut; registration ends with the service."""
import ctypes
from ctypes import wintypes
import threading
import time


class PauseHotkey:
    def __init__(self, state):
        self.state = state
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.registered = False
        self.thread = threading.Thread(target=self.run, daemon=True, name='panel-pause-hotkey')

    def start(self):
        self.thread.start()
        self.ready.wait(2)

    def run(self):
        user = ctypes.WinDLL('user32', use_last_error=True)
        self.registered = bool(user.RegisterHotKey(None, 0x5742, 0x4000 | 0x0001 | 0x0002, 0x7B))
        self.ready.set()
        if not self.registered:
            self.state.event('control', '全局暂停快捷键被占用，请使用面板按钮')
            return
        message = wintypes.MSG()
        try:
            while not self.stop_event.is_set():
                while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == 0x0312 and message.wParam == 0x5742:
                        self.state.transition('paused')
                        self.state.event('control', '通过 Ctrl + Alt + F12 暂停')
                time.sleep(.03)
        finally:
            user.UnregisterHotKey(None, 0x5742)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
