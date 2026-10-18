"""Cooperative cancellation at the actual Windows mouse/keyboard event boundary."""
import ctypes
import functools
import win32gui
import windows_mcp.uia as uia
import windows_mcp.uia.core as core
from control_state import ControlBlocked, INPUT_TOOLS


def install_input_guard(state):
    originals = {name: getattr(core, name) for name in ('keybd_event', 'mouse_event', 'SetCursorPos', 'SendInput')}
    held_keys = set()
    held_mouse = set()
    held_unicode = {}

    def ensure_target(lease, point=None):
        if lease.tool not in INPUT_TOOLS:
            return
        if win32gui.GetForegroundWindow() != lease.target:
            raise ControlBlocked('TARGET_NOT_FOREGROUND: input blocked. Focus the selected window and reobserve.')
        if point is not None:
            hwnd = win32gui.GetAncestor(win32gui.WindowFromPoint(tuple(point)), 2)
            if hwnd != lease.target:
                raise ControlBlocked('POINT_OUTSIDE_TARGET: input blocked. Use a fresh screenshot; the point may be covered by another window.')

    def keybd_event(key, scan, flags, extra):
        with state.lock:
            if flags & 2 and key in held_keys:
                held_keys.discard(key)
                return originals['keybd_event'](key, scan, flags, extra)
            lease = state.check()
            ensure_target(lease)
            result = originals['keybd_event'](key, scan, flags, extra)
            if not flags & 2:
                held_keys.add(key)
            return result

    mouse_pairs = {2: 4, 8: 16, 32: 64}

    def mouse_event(flags, x, y, data, extra):
        with state.lock:
            release_flag = next((up for up in held_mouse if flags & up), None)
            if release_flag is not None and not flags & (2 | 8 | 32):
                held_mouse.discard(release_flag)
                return originals['mouse_event'](flags, x, y, data, extra)
            lease = state.check()
            ensure_target(lease, core.GetCursorPos())
            result = originals['mouse_event'](flags, x, y, data, extra)
            for down, up in mouse_pairs.items():
                if flags & down:
                    held_mouse.add(up)
            return result

    def cursor(x, y):
        with state.atomic_input() as lease:
            ensure_target(lease, (x, y))
            return originals['SetCursorPos'](x, y)

    def send_input(*inputs):
        result = 0
        for item in inputs:
            with state.lock:
                keyboard = item.type == 1
                key = (item.union.ki.wVk, item.union.ki.wScan) if keyboard else None
                release = keyboard and item.union.ki.dwFlags & 2 and key in held_unicode
                if not release:
                    lease = state.check()
                    ensure_target(lease)
                result = originals['SendInput'](item)
                if keyboard:
                    if item.union.ki.dwFlags & 2:
                        held_unicode.pop(key, None)
                    else:
                        held_unicode[key] = item
        return result

    def cleanup():
        # Release only inputs injected by this service, never the user's keys.
        for key in list(held_keys):
            originals['keybd_event'](key, 0, 2, 0)
        for flag in list(held_mouse):
            originals['mouse_event'](flag, 0, 0, 0, 0)
        for item in list(held_unicode.values()):
            copy = core.INPUT()
            ctypes.memmove(ctypes.byref(copy), ctypes.byref(item), ctypes.sizeof(item))
            copy.union.ki.dwFlags |= 2
            originals['SendInput'](copy)
        held_keys.clear()
        held_mouse.clear()
        held_unicode.clear()

    state.cleanup = cleanup
    for name, replacement in [('keybd_event', keybd_event), ('mouse_event', mouse_event), ('SetCursorPos', cursor), ('SendInput', send_input)]:
        setattr(core, name, replacement)
        setattr(uia, name, replacement)

    # Top-level mutations that may not go through keyboard or mouse primitives.
    from windows_mcp.desktop.service import Desktop
    for name in ('bring_window_to_top', 'launch_app', 'app', 'type', 'multi_edit', 'multi_select'):
        original = getattr(Desktop, name)
        def wrap(fn):
            @functools.wraps(fn)
            def guarded(*args, **kwargs):
                state.check()
                return fn(*args, **kwargs)
            return guarded
        setattr(Desktop, name, wrap(original))
