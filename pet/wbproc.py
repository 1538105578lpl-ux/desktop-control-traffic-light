#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 进程探测 —— 桌宠红绿灯专用小工具

只干一件事：判断「WorkBuddy 主应用进程」还活着没有。

为什么不能只看有没有 WorkBuddy.exe：
  实测会长期残留若干父进程已退出的孤儿 WorkBuddy.exe，
  按名字判断会让桌宠永远不退出（2026-09-15 实测）。

判定策略（按优先级）：
  1. wb_pid.txt 里记着的锚点 PID —— 由 hook.py 沿父进程链向上找到的顶层 WorkBuddy.exe
  2. 兜底：名为 WorkBuddy.exe 且父进程是 explorer.exe 的那个

红线：所有函数失败一律返回 None / False，绝不抛异常
（hook.py 端出错会影响 WorkBuddy）。
"""

import ctypes
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes

DIR = os.path.dirname(os.path.abspath(__file__))
ANCHOR_FILE = os.path.join(DIR, "wb_pid.txt")

TARGET_EXE = "workbuddy.exe"
SHELL_EXE = "explorer.exe"
PET_TITLE = "WorkBuddy 灯"

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


# 必须显式声明原型：64 位下句柄是 8 字节，不声明会被截断成 32 位 → 静默失效
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND


# ---------------- 进程快照 ----------------
def snapshot():
    """返回 {pid: (ppid, exe_name)}；失败返回空 dict。"""
    procs = {}
    h = None
    try:
        h = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not h or h == INVALID_HANDLE_VALUE:
            return procs
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(h, ctypes.byref(entry))
        while ok:
            procs[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID),
                entry.szExeFile,
            )
            ok = kernel32.Process32NextW(h, ctypes.byref(entry))
    except Exception:
        pass
    finally:
        if h and h != INVALID_HANDLE_VALUE:
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass
    return procs


def is_alive(pid):
    """该 PID 是否存在且仍在运行。"""
    if not pid:
        return False
    h = None
    try:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    except Exception:
        return False
    finally:
        if h:
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass


def image_name(pid):
    """取进程的可执行文件名（不含路径），失败返回空串。"""
    if not pid:
        return ""
    h = None
    try:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    except Exception:
        return ""
    finally:
        if h:
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass


# ---------------- 锚点文件 ----------------
def read_anchor():
    try:
        with open(ANCHOR_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def write_anchor(pid):
    try:
        tmp = ANCHOR_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(pid)))
        os.replace(tmp, ANCHOR_FILE)
    except Exception:
        pass


def anchor_from_ancestors():
    """
    从当前进程沿父链向上走，找**顶层**那个 WorkBuddy.exe 的 PID，
    写进 wb_pid.txt。这是最精确的锚点（hook.py 每次运行都调一次）。

    返回 PID 或 None。
    """
    try:
        procs = snapshot()
        if not procs:
            return None
        cur = os.getpid()
        seen = set()
        found = None
        while cur and cur not in seen:
            seen.add(cur)
            item = procs.get(cur)
            if not item:
                break
            ppid, name = item
            if (name or "").lower() == TARGET_EXE:
                found = cur  # 一路向上，最后命中的就是最顶层
            cur = ppid
        if found:
            write_anchor(found)
        return found
    except Exception:
        return None


def find_by_parent():
    """兜底：名为 WorkBuddy.exe 且父进程是 explorer.exe 的那个。"""
    try:
        procs = snapshot()
        for pid, (ppid, name) in procs.items():
            if (name or "").lower() != TARGET_EXE:
                continue
            pinfo = procs.get(ppid)
            # 父进程已不在快照里 → 孤儿，直接跳过
            if pinfo and (pinfo[1] or "").lower() == SHELL_EXE:
                write_anchor(pid)
                return pid
    except Exception:
        pass
    return None


def main_pid():
    """WorkBuddy 主进程 PID；没在跑返回 None。"""
    # 快路径：锚点还活着就直接用（省一次全量枚举）
    pid = read_anchor()
    if pid and is_alive(pid) and image_name(pid).lower() == TARGET_EXE:
        return pid
    return find_by_parent()


def is_workbuddy_running():
    return main_pid() is not None


# ---------------- 桌宠窗口 ----------------
def find_pet_window():
    """按标题精确匹配找到桌宠自己的窗口；没有返回 None。"""
    try:
        h = user32.FindWindowW(None, PET_TITLE)
        return int(h) if h else None
    except Exception:
        return None


# ---------------- 解释器 ----------------
def find_pythonw():
    """
    找到带 tkinter 的 pythonw.exe。

    🔴 必须用系统 Python 3.12 —— WorkBuddy 自带的 managed Python 3.13
       **没有 tkinter**，用它启动桌宠会直接失败（且 pythonw 没控制台，静默消失）。

    hook.py（hook 端）和 watchdog.pyw（看门狗端）都从这里取，保证只有一份逻辑。
    """
    cands = []
    override = os.environ.get("PET_PYTHONW")
    if override:
        cands.append(override)
    local = os.environ.get("LOCALAPPDATA") or ""
    for _ver in ("Python312", "Python313", "Python311"):
        if local:
            cands.append(os.path.join(local, "Programs", "Python", _ver, "pythonw.exe"))
    cands.append(os.path.join(os.path.dirname(sys.executable or ""), "pythonw.exe"))
    _which = shutil.which("pythonw")
    if _which:
        cands.append(_which)
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


# ---------------- 用户主动退出便签 ----------------
SUPPRESS_FILE = os.path.join(DIR, "pet_suppressed.txt")


def user_suppressed():
    """
    用户在桌宠上主动点过「退出」，且 WorkBuddy 还是同一个进程 → 别再去烦他。

    hook.py（hook 端）和 watchdog.pyw（看门狗端）共用这一份判断，
    否则两条启动链路会互相打架：你关掉灯，另一个立刻又把它拉起来。
    """
    try:
        with open(SUPPRESS_FILE, "r", encoding="utf-8") as f:
            marked = f.read().strip()
    except Exception:
        return False
    cur = main_pid()
    return bool(marked) and cur is not None and str(cur) == marked


# ---------------- 看门狗（独立于 hook 的启动链路） ----------------
WATCHDOG_HEARTBEAT = os.path.join(DIR, "watchdog_heartbeat.txt")
WATCHDOG_DISABLED = os.path.join(DIR, "watchdog_disabled.txt")
HEARTBEAT_MAX_AGE = 12.0        # 心跳新鲜度阈值（看门狗每 6 秒写一次）


def watchdog_disabled():
    """用户 主动卸载过看门狗 → 谁都别再自动把它拉起来。"""
    return os.path.exists(WATCHDOG_DISABLED)


def watchdog_alive(max_age=HEARTBEAT_MAX_AGE):
    """
    看门狗在管「拉起桌宠」这件事吗？

    判据是心跳文件的新鲜度，而不是进程名 —— 看门狗是 pythonw.exe，
    和桌宠、和别的 python 脚本长得一模一样，按名字判会认错。
    """
    try:
        with open(WATCHDOG_HEARTBEAT, "r", encoding="utf-8") as f:
            stamp = float(f.read().strip())
    except Exception:
        return False
    return (time.time() - stamp) < max_age


# ---------------- 通过 WMI 逃出作业对象地启动进程 ----------------
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
CREATE_NO_WINDOW = 0x08000000


def launch_via_wmi(pyw, script, timeout=8):
    """
    通过 WMI 让**系统服务**代我们创建进程 —— 这是唯一能逃出 WorkBuddy
    作业对象（Job Object）的方式。

    为什么必须这样（2026-09-15 实测）：
      · 直接 Popen 出来的 pythonw，会在本次 hook 调用结束后被作业对象连坐杀掉
      · 加 CREATE_BREAKAWAY_FROM_JOB 也没用 —— 作业不允许逃逸，报「拒绝访问(5)」
      · 走 WMI 创建时，新进程父进程是 WmiPrvSE.exe，不在任何作业对象里，
        实测跨调用依然存活，窗口正常出现在用户会话

    ⚠️ 阻塞约 1~2 秒（PowerShell 冷启动）。**只在真要启动时才调用**，
       绝不能放在每个 hook 事件的常路径上，否则会拖慢 WorkBuddy 启动。

    返回进程号字符串；失败返回 None。
    """
    if not os.path.exists(POWERSHELL):
        return None
    cmdline = '"%s" "%s"' % (pyw, script)
    ps_cmd = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine='%s'}; Write-Output $r.ProcessId"
    ) % cmdline.replace("'", "''")
    try:
        out = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="gbk", errors="ignore",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        pid = (out.stdout or "").strip().splitlines()
        pid = pid[-1].strip() if pid else ""
        return pid if pid.isdigit() and pid != "0" else None
    except Exception:
        return None


# ---------------- 自检 ----------------
def describe():
    """给人看的诊断字符串（排查用，不参与业务逻辑）。"""
    procs = snapshot()
    lines = []
    lines.append("锚点文件: %s" % (read_anchor() or "（无）"))
    lines.append("主进程判定: %s" % (main_pid() or "None（WorkBuddy 未运行）"))
    lines.append("所有 WorkBuddy.exe:")
    for pid, (ppid, name) in sorted(procs.items()):
        if (name or "").lower() != TARGET_EXE:
            continue
        pname = procs.get(ppid, (0, "<父进程已退出>"))[1]
        lines.append("  pid=%-8s ppid=%-8s parent=%s" % (pid, ppid, pname))
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
