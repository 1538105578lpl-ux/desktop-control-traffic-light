"""Optional desktop controls drawn inside the existing rounded traffic-light window."""
import base64
import ctypes
import json
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from urllib.request import Request, urlopen

def _resolve_control_root():
    """定位控制服务目录：环境变量 CONTROL_ROOT 优先，其次同仓库的 server/。"""
    env = os.environ.get("CONTROL_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    for base in (here.parent / "server", here.parent):
        if (base / "panel_common.py").exists():
            return base
    return here.parent / "server"


CONTROL_ROOT = _resolve_control_root()
CONNECTION = CONTROL_ROOT / 'panel-runtime' / 'connection.json'
HOOK_RECORDS = CONTROL_ROOT / 'panel-runtime' / 'hook-events.json'
DIAG = Path(__file__).with_name('bridge_diag.log')


def diag(text):
    """把轮询状态变化落盘：pythonw.exe 没有 stderr，出问题只能靠文件排查。"""
    try:
        if DIAG.exists() and DIAG.stat().st_size > 200000:
            DIAG.unlink()
        with open(DIAG, 'a', encoding='utf-8') as f:
            f.write('%s\t%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), text))
    except Exception:
        pass


def request(path, payload=None, timeout=.8):
    connection = json.loads(CONNECTION.read_text(encoding='utf-8'))
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request('http://127.0.0.1:18765' + path, data=body,
                  headers={'X-Control-Token': connection['token'], 'Content-Type': 'application/json'})
    with urlopen(req, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if 'json' in response.headers.get('Content-Type', '') else raw


class PetControls:
    def __init__(self, pet):
        self.pet = pet
        self.value = None
        self.updated_at = 0
        self.image_bytes = None
        self.image_at = 0
        self.photo = None
        self.expanded = False
        self.layout = None
        self.pending = None
        self.error = ''
        self.last_mode = None
        self.alive = True
        self.buttons = []
        self.signature = None
        # 轮询自愈：代数号用于让旧的卡死线程自动退位，last_tick 用于识别"循环不再迭代"
        self.generation = 0
        self.started_at = 0.0
        self.last_tick = 0.0
        self.last_ok = 0.0
        self.stall_reported = False
        self._lock = threading.Lock()
        self._start_poll()
        threading.Thread(target=self.supervise, daemon=True).start()
        pet.root.after(0, self.no_activate)

    def _start_poll(self):
        """开一条新的轮询线程；旧线程下次醒来发现代数变了就自己退出。"""
        with self._lock:
            self.generation += 1
            generation = self.generation
            self.started_at = time.time()
            self.last_tick = 0.0
            self.stall_reported = False
            threading.Thread(target=self.poll, args=(generation,), daemon=True).start()
            return generation

    def supervise(self):
        """看护轮询：循环停止迭代超过 30 秒（线程死了或卡在内核等待里）就重建一条。

        判断依据是"循环有没有在转"（last_tick），不是"有没有取到数据"——
        控制服务本身宕机时循环仍在每 0.5 秒重试，那种情况只记日志、不重建。
        """
        diag('supervise 线程启动')
        while self.alive:
            time.sleep(5)
            if not self.alive:
                return
            try:
                now = time.time()
                idle = now - max(self.last_tick, self.started_at)
                if idle > 30:
                    diag('轮询停止迭代 %.0f 秒（代数 %d），自动重建线程' % (idle, self.generation))
                    self._start_poll()
                elif now - max(self.last_ok, self.started_at) > 30 and not self.stall_reported:
                    self.stall_reported = True
                    diag('控制服务连续 %.0f 秒无正常响应（线程仍在重试，非卡死）'
                         % (now - max(self.last_ok, self.started_at)))
            except Exception as exc:
                diag('supervise 异常：%s: %s' % (type(exc).__name__, exc))

    def no_activate(self):
        # Keep preview expansion and canvas button clicks from stealing the app's keyboard focus.
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user.GetAncestor.restype = ctypes.c_void_p
        user.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        hwnd = user.GetAncestor(self.pet.root.winfo_id(), 2)
        style = user.GetWindowLongW(hwnd, -20)
        user.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x80)

    def poll(self, generation):
        diag('poll 线程启动 代数=%d' % generation)
        last_err = None
        while self.alive and generation == self.generation:
            self.last_tick = time.time()
            try:
                value = request('/api/status')
                self.value, self.updated_at = value, time.time()
                self.last_ok = self.updated_at
                if last_err is not None:
                    diag('poll 恢复（上次错误：%s）' % last_err)
                    last_err = None
                if self.expanded and value.get('mode') != 'takeover':
                    try:
                        self.image_bytes = request('/api/preview?format=png')
                        self.image_at = time.time()
                    except Exception:
                        self.image_bytes = None
                else:
                    self.image_bytes = None
            except Exception as exc:
                msg = '%s: %s' % (type(exc).__name__, exc)
                if msg != last_err:
                    last_err = msg
                    diag('poll 失败：%s' % msg)
            time.sleep(.5)
        diag('poll 线程退出 代数=%d alive=%s' % (generation, self.alive))

    def command(self, action):
        if self.pending:
            return
        self.pending = action
        self.error = ''
        def send():
            try:
                self.value = request('/api/control', {'action': action}, timeout=2)
                self.updated_at = time.time()
            except Exception:
                self.error = '暂停未确认 · 请按 Ctrl+Alt+F12'
            finally:
                self.pending = None
        threading.Thread(target=send, daemon=True).start()
        if action == 'takeover':
            self.expanded = False

    def toggle_detail(self):
        self.expanded = not self.expanded
        self.signature = None

    def handle_release(self, event):
        for x1, y1, x2, y2, action in self.buttons:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                if action == 'detail':
                    self.toggle_detail()
                elif action == 'back':
                    from traffic_light import focus_workbuddy
                    focus_workbuddy()
                else:
                    self.command(action)
                return True
        return False

    def resize(self, width, height, card):
        pet = self.pet
        if self.layout == (width, height) and (not pet.root.winfo_ismapped() or
                (pet.root.winfo_width(), pet.root.winfo_height()) == (width, height)):
            return
        self.layout = width, height
        pet.root.attributes('-alpha', .98 if card else .93)
        pet.canvas.config(width=width, height=height)
        x = min(pet.root.winfo_x(), max(0, pet.root.winfo_screenwidth() - width - 8))
        y = min(pet.root.winfo_y(), max(0, pet.root.winfo_screenheight() - height - 8))
        pet.root.geometry(f'{width}x{height}+{max(0,x)}+{max(0,y)}')
        from traffic_light import round_rect_points
        pet.canvas.coords(pet.bg_item, *round_rect_points(0, 0, width-1, height-1, 19))
        center = width // 2
        for lamp, cx in zip(pet.lamps, (center-32, center, center+32)):
            pet.canvas.coords(lamp, cx-12, 16, cx+12, 40)
        pet.canvas.coords(pet.text_item, center, 56)
        pet.cur_state = None

    def text(self, x, y, text, color='#E6E6E6', size=9, anchor='w', width=0):
        return self.pet.canvas.create_text(x, y, text=text, fill=color,
            font=('Microsoft YaHei UI', size), anchor=anchor, width=width, tags='desktop')

    def button(self, x, y, width, title, action, primary=False):
        from traffic_light import round_rect
        c = self.pet.canvas
        round_rect(c, x, y, x+width, y+27, 8, fill='#665D34' if primary else '#353333',
                   outline='#7C7041' if primary else '#605654', tags='desktop')
        self.text(x+width/2, y+13, title, anchor='center', size=9)
        self.buttons.append((x, y, x+width, y+27, action))

    def render(self, hook):
        value = self.value
        if not value:
            return False
        mode = value.get('mode', 'idle')
        if mode != self.last_mode:
            if mode in {'idle', 'takeover'}:
                self.expanded = False
            self.last_mode = mode
        active = value.get('desktop_active', False)
        if not active and not self.expanded:
            self.pet.canvas.delete('desktop')
            self.buttons = []
            self.resize(176, 78, False)
            self.signature = None
            return False
        stale = time.time() - self.updated_at > 3
        signature = (value.get('sequence'), mode, stale, self.expanded, self.pending,
                     self.error, self.image_at, bool(self.image_bytes), time.time()-self.image_at < 3,
                     hook.get('working_count'), hook.get('waiting_count'),
                     self.pet.root.winfo_width(), self.pet.root.winfo_height())
        if signature == self.signature:
            return True
        self.signature = signature
        self.pet.canvas.delete('desktop')
        self.buttons = []
        width, height = (392, 437) if self.expanded else (246, 145)
        self.resize(width, height, True)
        paused = mode in {'paused', 'takeover', 'stopped'}
        waiting = mode == 'running' and (value.get('task', {}).get('status') == 'waiting' or
            hook.get('sessions', {}).get(value.get('owner_session'), {}).get('state') == 'waiting')
        labels = {'running': '正在操作电脑', 'paused': '电脑操作已暂停', 'takeover': '你正在接管电脑',
                  'stopped': '电脑操作已结束', 'idle': '本轮电脑操作结束'}
        label = '控制连接中断' if stale else labels.get(mode, '查看电脑操作')
        if waiting and not stale:
            label = '电脑操作等你确认'
        if self.pending:
            label = '正在暂停电脑操作' if self.pending == 'pause' else '正在交给你操作'
        self.pet.flash_until = 0
        self.pet.paint(1 if paused or waiting or stale or self.pending else (2 if mode == 'idle' else 0),
                       label, 'waiting' if paused or waiting or stale or self.pending else ('done' if mode == 'idle' else 'working'))
        action = self.error or ('回原任务说“继续”，将重新观察界面' if paused else value.get('current_action') or '准备观察界面')
        if not paused and value.get('target_title'):
            action += ' · ' + value['target_title']
        if mode == 'takeover':
            action = '直接操作原应用 · 回原任务说“继续”'
        elif mode == 'stopped':
            action = '回 WorkBuddy 开始新的电脑任务'
        self.text(width/2, 79, action[:34], '#D4CEBD', size=8, anchor='center')
        total = int(hook.get('working_count', 0)) + int(hook.get('waiting_count', 0))
        other = max(0, total - int(value.get('owner_session') in hook.get('sessions', {}) and
            hook['sessions'][value['owner_session']].get('state') in {'working', 'waiting'}))
        if other:
            self.text(width/2, 95, f'另外 {other} 个 WorkBuddy 任务仍在进行', '#BFB8A9', size=8, anchor='center')
        start = (width-214)/2
        if mode == 'running':
            self.button(start, 109, 61, '暂停', 'pause')
            self.button(start+70, 109, 70, '我来接管', 'takeover')
        elif mode == 'paused':
            self.button(start, 109, 61, '回任务', 'back', primary=True)
            self.button(start+70, 109, 70, '我来接管', 'takeover')
        else:
            resumable = mode in {'paused', 'takeover'}
            self.button(start, 109, 131, '回任务 · 说“继续”' if resumable else '回 WorkBuddy', 'back', primary=resumable)
        self.button(start+149, 109, 65, '收起' if self.expanded else '看画面', 'detail')
        if self.expanded:
            from traffic_light import round_rect
            c = self.pet.canvas
            round_rect(c, 16, 152, width-16, 361, 12, fill='#171919', outline='#6D5B50', tags='desktop')
            if self.image_bytes and time.time() - self.image_at < 3:
                try:
                    self.photo = tk.PhotoImage(data=base64.b64encode(self.image_bytes))
                    c.create_image(width/2, 256, image=self.photo, tags='desktop')
                    caption = '最近画面 · 窗口被遮挡时可能显示遮挡内容'
                except tk.TclError:
                    caption = '画面暂时不可用'
            else:
                self.text(width/2, 252, '等待窗口画面' if not stale else '连接中断 · 画面已停止更新', '#B7B9B9', anchor='center')
                caption = '只在展开时获取画面'
            self.text(18, 378, caption, '#C0B8AB', size=8)
            events = [e for e in value.get('events', []) if e.get('kind') in {'start', 'progress', 'blocked', 'error'}]
            for index, event in enumerate(events[-2:]):
                self.text(18, 398+index*19, str(event.get('message', ''))[:42], '#D2CABE', size=8)
        return True
