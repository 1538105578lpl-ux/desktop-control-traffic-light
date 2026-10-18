#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 桌宠红绿灯 —— 桌面悬浮窗

一个永远置顶、可拖动的小窗口，用三盏灯告诉你 WorkBuddy 现在什么情况：
  红灯 干活中   你发了指令，我在忙
  黄灯 等你一下 任务卡住了，需要你判断（授权框 / 要你选 / 等你超 60 秒）
  绿灯 干完了   我答完了 —— 会闪 6 下，并弹一条 Windows 通知
  橙闪 出错了   轮次因 API 错误结束

交互：
  单击  → 把 WorkBuddy 窗口切到最前
  右键  → 菜单（重置位置 / 暂停闪烁 / 退出）
  拖动  → 随便挪，位置自动记住

信号来自同目录的 state.json，由 hook.py 写入。
用 pythonw.exe 启动（无控制台黑框）。

外观（用户 2026-09-19 定）：
  19px 真圆角 —— 靠「色键透明」实现：canvas 底色设成 TRANSPARENT 怪色，
                 屏幕上该颜色处全透明，于是圆角矩形之外的四角就"消失"了。
  🔴 因此底色**只能**改 self.bg_item（那个圆角矩形）的 fill，
     **绝不能改 canvas 的 bg** —— 那是色键色，一改圆角就没了。
  亮灯时整个底色跟着状态变（红→暗红、黄→暖黄、绿→暗绿），
  绿灯完成闪烁时底色也跟着一亮一暗。
"""

import ctypes
import json
import math
import os
import subprocess
import sys
import time
import tkinter as tk
from ctypes import wintypes

# ---------- 路径 ----------
DIR = os.path.dirname(os.path.abspath(__file__))
if DIR not in sys.path:
    sys.path.insert(0, DIR)          # 保证 wbproc.py 能被找到（runpy 启动时也要）

STATE_FILE = os.path.join(DIR, "state.json")
POS_FILE = os.path.join(DIR, "pos.json")
SUPPRESS_FILE = os.path.join(DIR, "pet_suppressed.txt")   # 用户主动关掉后留的便签

import wbproc                        # noqa: E402  同目录的进程探测工具

# ---------- 外观 ----------
WIN_W, WIN_H = 176, 78

# 圆角
CORNER_R = 19          # 圆角半径（用户 2026-09-19 选定；自绘，不用系统原生那套）
ARC_STEPS = 16         # 每个圆角采样段数。19px / 16 段 ≈ 0.78px 弦长，肉眼看不出台阶

# 🔴 色键：画布底色用这个「怪色」→ 屏幕上凡这个颜色处一律全透明，
#    露出来的就是圆角。**它绝不能出现在任何绘制内容里**（底色/灯/文字/描边都要远离它）。
#    所以：改底色一律改 self.bg_item，**绝不能改 canvas 的 bg**。
TRANSPARENT = "#010203"

BG = "#1F1F1F"         # 闲着时、以及闪烁暗相位的底色（中性灰）

# 状态 → 底色（用户 选的「明显」强度）
# 推导：基准 #522626 = (82,38,38) = 中性灰 #1F1F1F(31,31,31) 叠加 (+51,+7,+7) 的红量；
#       黄＝同样的量加在 R/G → #524C26；绿＝加在 G/B → #264A31；橙取红黄之间 → #52401F
STATE_BG = {
    "working": "#522626",     # 暗红
    "waiting": "#524C26",     # 暖黄
    "done":    "#264A31",     # 暗绿
    "error":   "#52401F",     # 暗橙
    "idle":    "#1F1F1F",
}

LAMP_ON = ("#FF4B4B", "#FFC53D", "#4ADE80")   # 红 黄 绿（点亮）
LAMP_OFF_MIX = 0.22    # 灭灯 = 底色与灯亮色按此比例混合（动态算，见 blend()）
TEXT_COLOR = "#E6E6E6"
FONT = ("Microsoft YaHei UI", 9)

# 状态 → (灯索引, 状态文字)；灯索引 None = 全灭
#
# 语义（用户 2026-09-16 重定义）：
#   红 = 有任务在跑 ／ 黄 = **停下来了、在等你**（要你做选择，或任务暂停）／ 绿 = 全干完
STATE_STYLE = {
    "working": (0, "干活中"),
    "waiting": (1, "等你一下"),
    "done": (2, "本轮结束"),
    "error": (1, "出错了"),
    "idle": (None, "闲着"),
}

TICK_MS = 250          # 轮询间隔
FLASH_SECONDS = 1.2    # 完成/出错时闪烁时长
STALE_SECONDS = 3600   # state.json 超过 1 小时没更新 → 视为会话已结束

# ---------- 「任务暂停」判定 ----------
# 红点亮着、但 state.json 已经这么久没有任何新事件 → 任务其实停下来了
# （被中断 / 卡住 / 停了在等你），转黄灯提示。默认 5 分钟。
# 为什么敢这么判：正常干活时事件极密（实测每 8 秒就有一批 PermissionRequest），
# 5 分钟静默基本不可能是"还在跑"。
STALL_SECONDS = 300


def resolve_show(state, ts, now, show_stall=True):
    """
    纯函数：把 state.json 的内容 + 当前时刻，算出**真正该显示什么**。
    抽成纯函数是为了能脱掉 tkinter 直接单测（见 tests/test_state_logic.py 的写法）。

    规则：
      1. 太久没更新（> STALE_SECONDS）→ 会话早结束了，熄灯
      2. 红点亮着、但太久没任何事件（> STALL_SECONDS）→ 任务停了，转黄灯"停住了？"

    返回 (state, lamp_idx, label, stalled)
      · state 保持原值（stalled 时仍是 "working"），给闪烁判断用
      · stalled 只影响灯位和文案
    """
    if ts and (now - ts) > STALE_SECONDS:
        state = "idle"

    stalled = bool(show_stall and state == "working" and ts
                   and (now - ts) > STALL_SECONDS)
    if stalled:
        return "working", 1, "停住了？", True

    lamp_idx, label = STATE_STYLE.get(state, (None, "闲着"))
    return state, lamp_idx, label, False

# ---------- 跟随 WorkBuddy 自动退出 ----------
PROC_CHECK_EVERY = 8   # 每 8 个 tick（约 2 秒）查一次 WorkBuddy 主进程
MISS_LIMIT = 3         # 连续 3 次（约 6 秒）都看不到 → 判定已彻底退出，桌宠自己关掉

# ---------- Windows 原生调用 ----------
user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

# 必须声明函数原型：64 位下 HWND 是 8 字节指针，不声明会被截断成 32 位
# → 导致 GetWindowTextLengthW / SetForegroundWindow 收到无效句柄，静默失效
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
try:
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
except Exception:
    pass


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_class(hwnd):
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def find_workbuddy_window():
    """
    枚举可见窗口，找出 WorkBuddy 主窗口句柄。

    ⚠️ 必须排除桌宠自己 —— 桌宠标题是「WorkBuddy 灯」，含 "workbuddy" 字样，
       早先的版本会把它当成主窗口，导致「单击唤回」只是把自己拎到最前。
    优先级：class=Chrome_WidgetWin_1 且 title 恰好是 WorkBuddy ＞ 标题含 workbuddy。
    """
    exact, loose = [], []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if window_class(hwnd) == "TkTopLevel":   # 桌宠自己
            return True
        title = window_title(hwnd)
        if not title:
            return True
        low = title.strip().lower()
        if low == "workbuddy 灯":                # 双保险
            return True
        if low == "workbuddy":
            exact.append(hwnd)
        elif "workbuddy" in low:
            loose.append(hwnd)
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception:
        pass
    hits = exact or loose
    return hits[0] if hits else None


def focus_workbuddy():
    """把 WorkBuddy 窗口切到最前。"""
    hwnd = find_workbuddy_window()
    if not hwnd:
        return False
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 模拟一次 Alt 按键，绕开 Windows 的前台窗口锁定
        VK_MENU = 0x12
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def toast(title, message):
    """弹一条 Windows 系统通知（无第三方依赖）。"""
    ps = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    if not os.path.exists(ps):
        return
    xml = (
        "<toast><visual><binding template='ToastGeneric'>"
        f"<text>{title}</text><text>{message}</text>"
        "</binding></visual></toast>"
    )
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml(\"{xml}\");"
        "$t = New-Object Windows.UI.Notifications.ToastNotification $x;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('WorkBuddy').Show($t);"
    )
    try:
        subprocess.Popen(
            [ps, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except Exception:
        pass


def blend(c1, c2, t):
    """RGB 线性混色：t=0 取 c1，t=1 取 c2。用来算「熄灭的灯」该有多暗。"""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(
        int(round(a[k] + (b[k] - a[k]) * t)) for k in range(3))


def round_rect_points(x1, y1, x2, y2, r, steps=ARC_STEPS):
    """
    生成**真实圆角**矩形的顶点序列（四角各采样 steps 段圆弧，拼成一圈闭合）。

    ⚠️ 为什么不能再用 create_polygon(..., smooth=True)：
       Tk 的 smooth 是二次 B 样条，曲线**不经过控制点**，而是穿过控制多边形各边中点。
       实测：给 14px 的拐角控制点，实际只画出约 4px 圆角 ——
       这就是早先「看着像直角矩形」的真正原因（哪怕它和背景不同色也一样）。
       所以自己按圆弧采样，再 smooth=False 直接连线，才能拿到想要的 19px。
    """
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = []
    # 顺时针：右上 → 右下 → 左下 → 左上（圆心 + 起始角 angle°）
    for cx, cy, a0 in ((x2 - r, y1 + r, -90.0), (x2 - r, y2 - r, 0.0),
                       (x1 + r, y2 - r, 90.0), (x1 + r, y1 + r, 180.0)):
        for i in range(steps + 1):
            a = math.radians(a0 + 90.0 * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return pts


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """画一个真圆角矩形。返回 item id —— 调用方要留着改它的 fill。"""
    return canvas.create_polygon(round_rect_points(x1, y1, x2, y2, r),
                                 smooth=False, **kw)


class TrafficLight:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("WorkBuddy 灯")
        self.root.overrideredirect(True)                 # 无边框
        self.root.attributes("-topmost", True)           # 永远置顶
        try:
            self.root.attributes("-alpha", 0.93)
        except Exception:
            pass

        # 色键透明：画布底色 = TRANSPARENT → 屏幕上该颜色处全透明，
        # 于是圆角矩形之外的那四个角就"消失"了，露出真正的圆角。
        # 实测 Tk 8.6.15 支持，且与上面的 -alpha 不冲突（OS 层 flags=0x3，两者同时生效）。
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
            canvas_bg = TRANSPARENT
        except Exception:
            canvas_bg = BG          # 极少数不支持色键的环境 → 退化成直角黑窗，至少不崩

        self.root.configure(bg=canvas_bg)
        self.canvas = tk.Canvas(
            self.root, width=WIN_W, height=WIN_H,
            bg=canvas_bg, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        # ⚠️ 必须留下 item id：canvas 的 bg 已经被色键占用了，
        #    所以"底色"只能改这个圆角矩形的 fill。
        self.bg_item = round_rect(
            self.canvas, 0, 0, WIN_W - 1, WIN_H - 1, CORNER_R,
            fill=BG, outline="",
        )

        # 三盏灯
        self.lamps = []
        cy = 28
        for cx in (56, 88, 120):
            item = self.canvas.create_oval(
                cx - 12, cy - 12, cx + 12, cy + 12,
                fill=blend(BG, LAMP_ON[len(self.lamps)], LAMP_OFF_MIX),
                outline="",
            )
            self.lamps.append(item)

        self.text_item = self.canvas.create_text(
            WIN_W // 2, 56, text="闲着", fill=TEXT_COLOR,
            font=FONT, anchor="center",
        )

        # 状态
        self.cur_state = None
        self.last_ts = 0.0
        self.flash_until = 0.0
        self.mute_flash = False
        self.drag_origin = (0, 0)
        self.was_dragged = False

        # 跟随 WorkBuddy 自动退出
        self.auto_exit = True     # 右键菜单可关（调试用）
        self.seen_wb = False      # 门闩：从没见过 WorkBuddy 就永不自杀
        self.miss = 0             # 连续看不到主进程的次数
        self._proc_tick = 0

        # 红灯静默太久 → 转黄灯（"任务暂停"）。右键菜单可关
        self.show_stall = True

        self.restore_pos()
        self.bind_events()
        from desktop_bridge import PetControls
        self.desktop = PetControls(self)
        self.build_menu()
        self.root.after(TICK_MS, self.tick)

    # ---------- 位置记忆 ----------
    def restore_pos(self):
        x = y = None
        try:
            with open(POS_FILE, "r", encoding="utf-8") as f:
                p = json.load(f)
            x, y = int(p["x"]), int(p["y"])
        except Exception:
            pass
        if x is None:
            x = self.root.winfo_screenwidth() - WIN_W - 40
            y = int(self.root.winfo_screenheight() * 0.30)
        x = max(0, min(x, self.root.winfo_screenwidth() - WIN_W))
        y = max(0, min(y, self.root.winfo_screenheight() - WIN_H))
        self.root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

    def save_pos(self):
        try:
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y()}, f)
        except Exception:
            pass

    # ---------- 交互 ----------
    def bind_events(self):
        c = self.canvas
        c.bind("<Button-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Button-3>", self.on_right_click)
        self.root.bind("<Button-3>", self.on_right_click)

    def on_press(self, e):
        self.drag_origin = (e.x, e.y)
        self.was_dragged = False

    def on_drag(self, e):
        dx = e.x - self.drag_origin[0]
        dy = e.y - self.drag_origin[1]
        if abs(dx) > 2 or abs(dy) > 2:
            self.was_dragged = True
        self.root.geometry(f"+{self.root.winfo_x() + dx}+{self.root.winfo_y() + dy}")

    def on_release(self, e):
        if self.was_dragged:
            self.save_pos()
        elif not self.desktop.handle_release(e):
            focus_workbuddy()

    def on_right_click(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="打开 WorkBuddy", command=focus_workbuddy)
        self.menu.add_command(label="查看电脑操作详情", command=self.desktop.toggle_detail)
        self.menu.add_separator()
        self.menu.add_command(
            label=("恢复闪烁提醒" if self.mute_flash else "暂停闪烁提醒"),
            command=self.toggle_flash,
        )
        self.menu.add_command(
            label=("久停转黄灯：开" if self.show_stall else "久停转黄灯：关"),
            command=self.toggle_stall,
        )
        self.menu.add_command(label="重置位置", command=self.reset_pos)
        self.menu.add_separator()
        self.menu.add_command(
            label=("随 WorkBuddy 自动退出：开" if self.auto_exit else "随 WorkBuddy 自动退出：关"),
            command=self.toggle_auto_exit,
        )
        self.menu.add_separator()
        self.menu.add_command(
            label="退出",
            command=lambda: self.quit_app(user_initiated=True),
        )

    def toggle_auto_exit(self):
        self.auto_exit = not self.auto_exit
        self.miss = 0
        self.build_menu()

    def toggle_flash(self):
        self.mute_flash = not self.mute_flash
        self.build_menu()

    def toggle_stall(self):
        """红灯静默太久要不要转黄灯（"任务暂停"提示）。"""
        self.show_stall = not self.show_stall
        self.cur_state = None          # 强制下一 tick 重绘
        self.build_menu()

    def reset_pos(self):
        try:
            os.remove(POS_FILE)
        except Exception:
            pass
        self.restore_pos()
        self.save_pos()

    # ---------- 跟随 WorkBuddy 存亡 ----------
    def check_workbuddy(self):
        """
        每约 2 秒查一次 WorkBuddy 主进程。
        连续 MISS_LIMIT 次（约 6 秒）都看不到 → 判定已彻底退出，桌宠自己关掉。

        seen_wb 门闩很关键：手动双击启动桌宠时 WorkBuddy 可能根本没开，
        没这道闩桌宠会启动即自杀。
        """
        alive = False
        try:
            alive = wbproc.is_workbuddy_running()
        except Exception:
            alive = False

        if alive:
            if not self.seen_wb:
                self.seen_wb = True
            self.miss = 0
            return

        if not self.seen_wb:
            return          # 还没见过 WorkBuddy → 不自杀

        self.miss += 1
        if self.miss >= MISS_LIMIT:
            self.quit_app()

    def quit_app(self, user_initiated=False):
        """
        关掉桌宠。

        user_initiated=True（右键菜单点的）会留一张"我主动关的"便签，
        免得下一条消息 hook 又把窗口拉起来 —— 便签里记着当前 WorkBuddy 主进程 PID，
        下次 WorkBuddy 重启（PID 变了）就自动失效，桌宠照常自动启动。
        """
        try:
            self.save_pos()
        except Exception:
            pass
        if user_initiated:
            try:
                pid = wbproc.main_pid() or 0
                with open(SUPPRESS_FILE, "w", encoding="utf-8") as f:
                    f.write(str(pid))
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- 状态轮询 ----------
    def read_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def paint(self, lamp_idx, label, bg_state="idle", dim=False):
        """
        重绘整个窗口。

        bg_state  当前状态（决定底色）
        dim       True = 闪烁的暗相位：底色回落中性灰，整窗跟着一亮一暗

        ⚠️ 底色只能改 self.bg_item 的 fill，**绝不能碰 canvas.bg** ——
           那是色键色，一改圆角就没了（还会在画面里挖出错位的透明洞）。
        """
        bg = BG if dim else STATE_BG.get(bg_state, BG)
        self.canvas.itemconfig(self.bg_item, fill=bg)
        for i, item in enumerate(self.lamps):
            # 灭灯色跟着底色动态算：底色变暗红后，写死的旧暗色会跟底糊成一片
            self.canvas.itemconfig(
                item,
                fill=(LAMP_ON[i] if i == lamp_idx else blend(bg, LAMP_ON[i], LAMP_OFF_MIX)),
            )
        self.canvas.itemconfig(self.text_item, text=label)

    def tick(self):
        # 跟随 WorkBuddy 存亡：每 8 个 tick（约 2 秒）查一次，不额外开线程
        self._proc_tick += 1
        if self.auto_exit and self._proc_tick % PROC_CHECK_EVERY == 0:
            self.check_workbuddy()

        data = self.read_state()
        if self.desktop.render(data):
            self.last_ts = float(data.get("ts") or 0)
            self.root.after(TICK_MS, self.tick)
            return
        state = (data.get("state") or "idle").strip()
        ts = float(data.get("ts") or 0)

        state, lamp_idx, label, stalled = resolve_show(
            state, ts, time.time(), self.show_stall)
        working_count = int(data.get("working_count") or 0)
        waiting_count = int(data.get("waiting_count") or 0)
        if state == "working" and working_count > 1 and not stalled:
            label = f"{working_count} 个任务在进行"
        elif state == "waiting" and working_count:
            label = f"等你确认 · 另有 {working_count} 个进行中"
        render_state = ("stalled" if stalled else state, label)
        # 「停住了？」要跟黄灯同一个底色（STATE_BG 里没有 "stalled"，
        # 不映射的话停住时会掉回黑底，看着像熄灯）
        bg_state = "waiting" if stalled else state

        # 新一轮完成/出错 → 触发闪烁 + 通知
        if self.last_ts and ts and ts != self.last_ts and state in ("done", "error"):
            # A session closing is not another completion, and startup is silent.
            if data.get("event") not in ("Stop", "StopFailure"):
                self.last_ts = ts
            else:
                self.flash_until = time.time() + FLASH_SECONDS
                if not self.mute_flash:
                    if state == "done":
                        detail = (data.get("detail") or "").strip()
                        task = (data.get("cwd") or "").split("\\")[-1] or "任务"
                        toast("WorkBuddy 本轮结束", f"{task} 已结束本轮回复，点击灯查看结果")
                    else:
                        toast("WorkBuddy 出错了", "这一轮因 API 错误中断，去看看")
        self.last_ts = ts or self.last_ts

        # 闪烁期间灯在"亮/灭"之间跳，底色同步一亮一暗（用户 2026-09-19 要的）
        flashing = time.time() < self.flash_until
        dim = False
        if flashing:
            phase = int((self.flash_until - time.time()) * 4) % 2 == 0
            if lamp_idx is not None and not phase:
                dim = True
                lamp_idx = None
        elif self.flash_until:
            # 闪烁刚结束：清标记并强制重绘一次，免得暗相位残留在屏幕上
            self.flash_until = 0.0
            self.cur_state = None

        if render_state != self.cur_state or flashing:
            self.paint(lamp_idx, label, bg_state, dim)
            self.cur_state = render_state

        self.root.after(TICK_MS, self.tick)

    def run(self):
        self.root.mainloop()


def find_own_window():
    """找已经存在的桌宠窗口（按标题精确匹配）。"""
    return wbproc.find_pet_window()


def bring_to_front(hwnd):
    """把已有窗口提到最前，并让它闪一下，给人反馈。"""
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


if __name__ == "__main__":
    def _boot_log(msg):
        """启动链路日志：区分「根本没启动」和「启动了但没显示出来」。"""
        try:
            with open(os.path.join(DIR, "startup_diag.log"), "a", encoding="utf-8") as f:
                f.write("%s\t[桌宠] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass

    _boot_log("进程启动 pid=%d 会话=%s" % (os.getpid(), os.environ.get("SESSIONNAME") or "?"))

    try:
        old = find_own_window()
        if old:
            # 已经在跑 → 不静默退出，而是把老窗口拎到最前，让你看得见
            _boot_log("已有实例 hwnd=%s，前置后退出" % old)
            bring_to_front(old)
            sys.exit(0)

        app = TrafficLight()

        def _selfcheck():
            """1.5 秒后确认窗口真能被系统枚举到 —— 排除窗口站隔离之类的暗坑。"""
            try:
                h = wbproc.find_pet_window()
                if h:
                    _boot_log("自检通过：窗口可被系统枚举 hwnd=%s" % h)
                else:
                    _boot_log("自检失败：窗口建了却枚举不到（疑似窗口站隔离）")
            except Exception as e:
                _boot_log("自检异常 %r" % (e,))

        app.root.after(1500, _selfcheck)
        app.run()
    except SystemExit:
        raise
    except Exception:
        # pythonw 没有控制台，异常会凭空消失 —— 一律落盘，方便排查
        import traceback
        try:
            with open(os.path.join(DIR, "error_log.txt"), "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 50 + "\n")
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise
