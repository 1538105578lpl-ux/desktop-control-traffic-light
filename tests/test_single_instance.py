#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：并发启动 5 个桌宠，验证单实例互斥体真的只让一个活下来。

为什么必须并发测（2026-10-19）：
  原实现靠"先查有没有窗口，没有再建窗口"防重复，这不是原子操作。
  两个启动源在同一秒各拉一次时，先启动的那个还在建窗口（要 3~5 秒），
  后启动的查不到窗口，于是两个都建了 —— 屏幕上出现两个红绿灯。
  所以修复后必须用"同一瞬间拉起多个"来验，串行启动测不出问题。

跑法：python tests/test_single_instance.py
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(DIR), "pet", "traffic_light.py")
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(PYW):
    PYW = sys.executable


def enum_windows():
    u = ctypes.windll.user32
    u.FindWindowExW.restype = ctypes.c_void_p
    u.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.LPCWSTR, wt.LPCWSTR]
    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    out, h = [], None
    while True:
        h = u.FindWindowExW(None, h, "TkTopLevel", None)
        if not h:
            break
        pid = wt.DWORD(0)
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        r = wt.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        out.append((pid.value, r.left, r.top))
    return out


def pet_pids():
    return sorted(p for p, _, _ in enum_windows())


def kill(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


base = pet_pids()
print("启动前已有桌宠 pid:", base or "无")
for p in base:
    kill(p)
time.sleep(1.5)

print("同一瞬间拉起 5 个 ...")
procs = [subprocess.Popen([PYW, SCRIPT], cwd=DIR,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
         for _ in range(5)]
spawned = [p.pid for p in procs]
print("  子进程 pid:", spawned)

for i in range(12):
    time.sleep(1)
    now = pet_pids()
    print("  t+%2ds  窗口数=%d  pid=%s" % (i + 1, len(now), now))
    if i >= 6 and len(now) == 1:
        break

alive = [p for p in procs if p.poll() is None]
final = pet_pids()
print()
print("存活子进程 = %d / 5  (%s)" % (len(alive), [p.pid for p in alive]))
print("屏幕上桌宠窗口 = %d  (pid=%s)" % (len(final), final))
print()
if len(final) == 1 and len(alive) <= 1:
    print("结论: ✅ 单实例生效")
    sys.exit(0)
print("结论: ❌ 仍然多开")
sys.exit(1)
