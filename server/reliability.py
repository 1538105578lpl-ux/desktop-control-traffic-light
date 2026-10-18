"""Local repair layer; original third-party package remains unchanged on disk."""
import json
import os
import time
from pathlib import Path

import win32con
import win32gui
import win32process
from psutil import Process
import windows_mcp.uia as uia
from windows_mcp.desktop.service import Desktop
from windows_mcp.desktop.views import Window, Status, Browser
from windows_mcp.tree.views import BoundingBox

WECHAT_EXE = Path(r'E:\Program Files\Tencent\Weixin\Weixin.exe')
ALIASES = {'微信': {'weixin.exe', 'wechat.exe'}, 'wechat': {'weixin.exe', 'wechat.exe'},
           'weixin': {'weixin.exe', 'wechat.exe'}}
ORIGINAL = {}

def rows(include_hidden=False):
    result = []
    def collect(hwnd, _):
        try:
            visible = bool(win32gui.IsWindowVisible(hwnd))
            if not include_hidden and not visible:
                return
            if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = Process(pid)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right <= left or bottom <= top:
                return
            result.append(dict(handle=hwnd, pid=pid, process=proc.name(), title=title,
                               visible=visible, minimized=bool(win32gui.IsIconic(hwnd)),
                               rect=[left, top, right, bottom], foreground=hwnd == win32gui.GetForegroundWindow()))
        except Exception:
            return
    win32gui.EnumWindows(collect, None)
    return result

def choose(candidates, name=None, process_id=None, window_handle=None):
    if window_handle is not None:
        matched = [r for r in candidates if r['handle'] == window_handle]
    elif process_id is not None:
        matched = [r for r in candidates if r['pid'] == process_id]
    elif name:
        query = name.casefold()
        aliases = ALIASES.get(query, {query, query if query.endswith('.exe') else query + '.exe'})
        matched = [r for r in candidates if r['title'].casefold() == query or r['process'].casefold() in aliases]
        if not matched:
            matched = [r for r in candidates if query in r['title'].casefold()]
    else:
        raise ValueError('Specify name, process_id, or window_handle from WindowList.')
    if process_id is not None:
        matched = [r for r in matched if r['pid'] == process_id]
    if not matched:
        raise ValueError('Application/window not found. Refresh WindowList; do not guess coordinates.')
    # Visible main windows take precedence over hidden helper/login windows.
    visible = [r for r in matched if r['visible']]
    if visible:
        matched = visible
    if len(matched) != 1:
        raise ValueError('Ambiguous windows; select a window_handle: ' + json.dumps(matched, ensure_ascii=False))
    return matched[0]

def as_window(row, depth=0):
    l, t, r, b = row['rect']
    status = Status.MINIMIZED if row['minimized'] else (Status.NORMAL if row['visible'] else Status.HIDDEN)
    return Window(name=row['title'], is_browser=Browser.has_process(row['process']), depth=depth,
                  status=status, bounding_box=BoundingBox(left=l, top=t, right=r, bottom=b,
                  width=r-l, height=b-t), handle=row['handle'], process_id=row['pid'])

def get_windows(self, controls_handles=None):
    # Enumerate native top-level windows; WindowPattern/CanMaximize is not required.
    found = [as_window(row, i) for i, row in enumerate(rows())]
    return found, {window.handle for window in found}

def find_window(self, name, refresh_state=False):
    try:
        return as_window(choose(rows(include_hidden=True), name=name)), ''
    except ValueError as exc:
        return None, str(exc)

def focus(self, handle):
    if not win32gui.IsWindow(handle):
        raise ValueError('Stale window_handle; refresh WindowList.')
    if not win32gui.IsWindowVisible(handle):
        win32gui.ShowWindow(handle, win32con.SW_SHOW)
    ORIGINAL['bring_window_to_top'](self, handle)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if win32gui.GetForegroundWindow() == handle:
            return
        time.sleep(.05)
    raise RuntimeError('FOCUS_NOT_ACQUIRED: no input was sent. Refocus and observe again.')

def launch(self, name):
    if name.casefold() in ALIASES:
        candidates = [r for r in rows(True) if r['process'].casefold() in ALIASES[name.casefold()]]
        if candidates:
            row = choose(candidates, name=name)
            self.bring_window_to_top(row['handle'])
            return 'Existing WeChat window activated; verify login state with Snapshot.', 0, row['pid']
        if not WECHAT_EXE.is_file():
            return 'Configured WeChat executable is missing.', 1, 0
        import subprocess
        process = subprocess.Popen([str(WECHAT_EXE)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 'WeChat launched; observe its login/application window before input.', 0, process.pid
    return ORIGINAL['launch_app'](self, name)

def editable(control):
    return control is not None and control.ControlTypeName in {'EditControl', 'DocumentControl'}

def read_value(control):
    try:
        pattern = control.GetValuePattern()
        if pattern is not None:
            return pattern.Value
    except Exception:
        pass
    try:
        pattern = control.GetTextPattern()
        if pattern is not None:
            return pattern.DocumentRange.GetText(-1)
    except Exception:
        pass
    return None

def normalize(text):
    return text.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')

def guarded_type(self, loc, text, caret_position='idle', clear=False, press_enter=False):
    clear = clear is True or str(clear).lower() == 'true'
    submit = press_enter is True or str(press_enter).lower() == 'true'
    if '\x00' in text:
        raise ValueError('NUL characters cannot be pasted.')
    point = tuple(loc)
    target_hwnd = win32gui.GetAncestor(win32gui.WindowFromPoint(point), 2)
    if target_hwnd != win32gui.GetForegroundWindow():
        raise RuntimeError('TARGET_NOT_FOREGROUND: activate the target window and take a fresh Snapshot first.')
    uia.Click(*point)
    time.sleep(.15)
    control = uia.GetFocusedControl()
    if win32gui.GetForegroundWindow() != target_hwnd or not editable(control):
        raise RuntimeError('EDIT_FOCUS_REQUIRED: no text entered; click an observed editable field first.')
    _, target_pid = win32process.GetWindowThreadProcessId(target_hwnd)
    if control.ProcessId != target_pid:
        raise RuntimeError('FOCUS_CHANGED: no text entered.')
    rect = control.BoundingRectangle
    if not (rect.left <= point[0] <= rect.right and rect.top <= point[1] <= rect.bottom):
        raise RuntimeError('FOCUS_TARGET_MISMATCH: coordinates do not point inside the focused editor.')
    before = read_value(control)
    if caret_position == 'start':
        uia.SendKeys('{Ctrl}{Home}')
    elif caret_position == 'end':
        uia.SendKeys('{Ctrl}{End}')
    if win32gui.GetForegroundWindow() != target_hwnd:
        raise RuntimeError('FOCUS_CHANGED: input cancelled.')
    if clear:
        uia.SendKeys('{Ctrl}a')
    # Literal clipboard transfer: newline is never synthesized as an Enter key.
    import win32clipboard
    previous_text = None
    previous_has_text = False
    try:
        previous_has_text = win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT)
        if previous_has_text:
            previous_text = uia.GetClipboardText()
    except Exception:
        pass
    uia.SetClipboardText(text)
    sequence = win32clipboard.GetClipboardSequenceNumber()
    try:
        if win32gui.GetForegroundWindow() != target_hwnd:
            raise RuntimeError('FOCUS_CHANGED: paste cancelled.')
        uia.SendKeys('{Ctrl}v', waitTime=.2)
        deadline = time.monotonic() + 2
        verified = False
        after = None
        while time.monotonic() < deadline:
            after = read_value(control)
            if after is not None:
                verified = normalize(after) == normalize(text) if clear else normalize(text) in normalize(after)
                if verified:
                    break
            time.sleep(.1)
        result = dict(inserted=True, verified=verified, method='literal_clipboard_paste',
                      window_handle=target_hwnd, text_length=len(text), submitted=False,
                      verification='exact' if verified and clear else ('text_present' if verified else 'unavailable_or_mismatch'))
        if submit:
            if not verified:
                raise RuntimeError('INPUT_UNVERIFIED: text may have been pasted; submission was NOT attempted. Observe before retrying.')
            if win32gui.GetForegroundWindow() != target_hwnd or uia.GetFocusedControl().ProcessId != target_pid:
                raise RuntimeError('FOCUS_CHANGED: text pasted but submission was NOT attempted.')
            uia.SendKeys('{Enter}')
            result['submitted'] = True
            result['delivery_verified'] = False
        self._last_type_result = result
        return result
    finally:
        # Do not overwrite a clipboard changed concurrently by the user.
        if previous_has_text and previous_text is not None and win32clipboard.GetClipboardSequenceNumber() == sequence:
            time.sleep(.1)
            uia.SetClipboardText(previous_text)

def register_extra(mcp, get_desktop):
    @mcp.tool(name='WindowList', description='Fresh native Windows window list with handle, PID, process, title and bounds. Finds WeChat even when its title is a contact name. Use handle to disambiguate. Read only.')
    def window_list(include_hidden: bool = False) -> list[dict]:
        return rows(include_hidden)

    @mcp.tool(name='FocusWindow', description='Activate a freshly observed window by handle, PID, process name or title. Verifies foreground activation; refuses ambiguous matches. Take a fresh Snapshot after this.')
    def focus_window(name: str | None = None, process_id: int | None = None, window_handle: int | None = None) -> dict:
        row = choose(rows(True), name, process_id, window_handle)
        get_desktop().bring_window_to_top(row['handle'])
        current = choose(rows(True), window_handle=row['handle'])
        return dict(current, foreground_verified=True)

    @mcp.tool(name='ReadFocusedInput', description='Read focused editor value to verify literal input without sending. Returns null when the app does not expose editable content. Does not read clipboard.')
    def read_focused_input() -> dict:
        control = uia.GetFocusedControl()
        return dict(window_handle=win32gui.GetForegroundWindow(), editable=editable(control),
                    control_type=control.ControlTypeName if control else None,
                    value=read_value(control) if editable(control) else None)

def install():
    if ORIGINAL:
        return
    for name, replacement in [('get_windows', get_windows), ('_find_window_by_name', find_window),
                               ('bring_window_to_top', focus), ('launch_app', launch), ('type', guarded_type)]:
        ORIGINAL[name] = getattr(Desktop, name)
        setattr(Desktop, name, replacement)
    import windows_mcp.tools as tools
    original_register = tools.register_all
    def register_all(mcp, *, get_desktop, get_analytics):
        original_register(mcp, get_desktop=get_desktop, get_analytics=get_analytics)
        register_extra(mcp, get_desktop)
    tools.register_all = register_all
