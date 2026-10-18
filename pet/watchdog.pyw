#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 桌宠红绿灯 —— 开机看门狗（Watchdog）

【为什么需要它 · 2026-09-19 用户 报障后加的】
  原来「打开 WorkBuddy 桌宠自动出现」完全押在 WorkBuddy 启动时的 Setup hook 上。
  实测那条链路要 5 秒才出窗口：

      11:15:31  Setup hook 触发
      11:15:32  WMI 返回 pid（PowerShell 冷启动约 1 秒）
      11:15:33  桌宠进程启动
      11:15:36  窗口才可被系统枚举

  用户 在这 5 秒内看屏幕 → 以为「没显示」。更致命的是：万一 Setup hook 那次没触发，
  灯就永远不出现，没有任何兜底。

  看门狗把「发现 WorkBuddy → 拉起灯」这件事从 hook 手里接过来。
  它是**开机自启的独立进程**，与 hook 触发时机完全无关，轮询间隔 1.5 秒。

【为什么看门狗可以直接 Popen，不需要 WMI 那套骚操作】
  WMI 只在 hook.py 里才必要 —— 因为 hook 进程被 WorkBuddy 关在作业对象（Job Object）里，
  它派生的子进程会在 hook 调用结束时被连坐回收（实测 CREATE_BREAKAWAY_FROM_JOB 报「拒绝访问」）。
  而看门狗是开机时由 explorer.exe 拉起的，**不在任何作业对象里**，
  它 Popen 出来的桌宠自然也不会被回收。

【行为】
  · 灯在跑                    → 什么都不做（灯有它自己的「WorkBuddy 退出就自杀」逻辑）
  · WorkBuddy 在跑 + 灯不在   → 拉起灯
  · WorkBuddy 不在            → 什么都不做
  · 用户 右键关过灯             → 尊重 pet_suppressed.txt，同一个 WorkBuddy 进程下不打扰
  · 每 6 秒写一次心跳          → hook.py 看到心跳新鲜就立刻放行，
                               不再用 WMI 阻塞 WorkBuddy 启动

【红线】
  1. 任何异常都必须被吞掉 —— 看门狗崩了就等于桌宠永远不会自动出现
  2. 单实例（命名互斥体）—— 双保险自启（注册表 + 启动文件夹）不会跑出两个看门狗
  3. 绝不调用 wbproc.main_pid() 以外的重活；轮询必须极便宜（正常路径 1 次 FindWindowW）
"""

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

DIR = os.path.dirname(os.path.abspath(__file__))
if DIR not in sys.path:
    sys.path.insert(0, DIR)          # 保证能 import 同目录的 wbproc.py

import wbproc                        # noqa: E402

TRAFFIC_LIGHT = os.path.join(DIR, "traffic_light.py")
HEARTBEAT = os.path.join(DIR, "watchdog_heartbeat.txt")
PID_FILE = os.path.join(DIR, "看门狗PID.txt")
LOG_FILE = os.path.join(DIR, "watchdog.log")

POLL_SECONDS = 1.5          # 轮询间隔
HEARTBEAT_EVERY = 4         # 每 N 轮写一次心跳（4 × 1.5s = 6 秒）
LAUNCH_COOLDOWN = 8.0       # 拉起后给窗口建立的宽限期，避免连拉

MUTEX_NAME = "Local\\WorkBuddyPetWatchdog"
ERROR_ALREADY_EXISTS = 183

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE

# 句柄必须长期持有：一旦被 GC 回收，互斥体就释放了，第二个实例会趁虚而入
_MUTEX = None

_last_launch = 0.0


# ---------------- 日志（滚动保留最近 80 行） ----------------
def wlog(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 32 * 1024:
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-80:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------- 单实例 ----------------
def acquire_single_instance():
    """拿到互斥体返回 True；已经有实例在跑返回 False。"""
    global _MUTEX
    try:
        h = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not h:
            return True                 # 拿不到互斥体（权限等）→ 宁可放行也别静默不干活
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False
        _MUTEX = h
        return True
    except Exception:
        return True


# ---------------- 心跳 & PID ----------------
def write_heartbeat():
    """
    写心跳时间戳。hook.py 靠它判断「看门狗在管启动这件事」，
    从而立刻放行、不再用 WMI 阻塞 WorkBuddy。

    ⚠️ 用原子替换，避免 hook.py 读到写了一半的数字。
    """
    try:
        tmp = HEARTBEAT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("%.1f" % time.time())
        os.replace(tmp, HEARTBEAT)
    except Exception:
        pass


def write_pid_file():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


# ---------------- 拉起桌宠 ----------------
def launch_pet():
    global _last_launch
    _last_launch = time.time()

    pyw = wbproc.find_pythonw()
    if not pyw or not os.path.exists(TRAFFIC_LIGHT):
        wlog("放弃启动：pythonw=%s traffic_light=%s"
             % (pyw, os.path.exists(TRAFFIC_LIGHT)))
        return
    try:
        subprocess.Popen(
            [pyw, TRAFFIC_LIGHT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        wlog("已拉起桌宠（WorkBuddy pid=%s）" % (wbproc.main_pid() or "?"))
    except Exception as e:
        wlog("拉起失败: %r" % (e,))


# ---------------- 一轮 ----------------
def tick():
    # 1. 灯在跑？→ 收工。这是最便宜的一步，也是绝大多数轮次的实际路径。
    #    灯的「WorkBuddy 退出就自杀」由它自己负责，看门狗不插手。
    if wbproc.find_pet_window():
        return

    # 2. 灯不在，WorkBuddy 也不在 → 本来就不该有灯
    if not wbproc.is_workbuddy_running():
        return

    # 3. 用户 主动关过灯（且 WorkBuddy 没换进程）→ 尊重他的选择
    if wbproc.user_suppressed():
        return

    # 4. 刚拉过，给窗口一点建立时间
    if time.time() - _last_launch < LAUNCH_COOLDOWN:
        return

    # 5. 该出手了
    launch_pet()


def main():
    if not acquire_single_instance():
        wlog("已有看门狗在跑，本实例退出")
        return
    write_pid_file()
    write_heartbeat()
    wlog("看门狗启动 pid=%d 轮询=%.1fs" % (os.getpid(), POLL_SECONDS))

    rounds = 0
    while True:
        try:
            rounds += 1
            if rounds % HEARTBEAT_EVERY == 1:
                write_heartbeat()
            tick()
        except Exception as e:
            wlog("tick 异常: %r" % (e,))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # pythonw 没有控制台，异常会凭空消失 —— 兜底落盘
        wlog("看门狗主线异常退出: %r" % (e,))
        raise
