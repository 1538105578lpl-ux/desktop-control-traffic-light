#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 桌宠红绿灯 —— 看门狗自启管理

为什么需要开机自启（2026-09-19）：
  原来「打开 WorkBuddy 桌宠自动出现」完全押在 WorkBuddy 的 Setup hook 上，
  实测要 5 秒才出窗口；万一那次 hook 没触发，灯就永远不出现。
  看门狗是开机常驻的独立进程，1.5 秒轮询一次，与 hook 时机完全无关。

自启双保险（两个都装，靠命名互斥体保证不会跑出两个看门狗）：
  ① 注册表  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
            → 最标准的方式，登录即启动
  ② 启动文件夹  shell:startup\\启动桌宠看门狗.vbs
            → VBS 启动，零窗口闪烁；注册表被安全软件清了也还有这一层

用法（用系统 Python 3.12 跑）：
    python manage_watchdog.py status      看状态（不带参数也是这个）
    python manage_watchdog.py install     装自启 + 立即启动看门狗
    python manage_watchdog.py uninstall   卸自启 + 停掉看门狗（并留停用标记，hook 也不会再补）
"""

import ctypes
import os
import subprocess
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
if DIR not in sys.path:
    sys.path.insert(0, DIR)

import wbproc                        # noqa: E402

WATCHDOG = os.path.join(DIR, "watchdog.pyw")
TRAFFIC_LIGHT = os.path.join(DIR, "traffic_light.py")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "WorkBuddyPetWatchdog"

STARTUP_DIR = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
VBS_FILE = os.path.join(STARTUP_DIR, "启动桌宠看门狗.vbs")

TASKKILL = r"C:\Windows\System32\taskkill.exe"

VBS_TEMPLATE = (
    "' WorkBuddy 桌宠看门狗 —— 开机自启\r\n"
    "' 由 manage_watchdog.py 生成。想取消：删掉本文件，或跑\r\n"
    "'     python manage_watchdog.py uninstall\r\n"
    "Set ws = CreateObject(\"WScript.Shell\")\r\n"
    "ws.Run \"\"\"%s\"\" \"\"%s\"\"\", 0, False\r\n"
)


# ---------------- 注册表 ----------------
def reg_get():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            v, _ = winreg.QueryValueEx(k, RUN_NAME)
            return v
        finally:
            winreg.CloseKey(k)
    except Exception:
        return None


def reg_set(value):
    import winreg
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(k)


def reg_del():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(k, RUN_NAME)
            return True
        finally:
            winreg.CloseKey(k)
    except Exception:
        return False


# ---------------- 启动文件夹 VBS ----------------
def write_vbs(pyw):
    """
    ⚠️ 必须写成 **GBK + CRLF**：VBS 由 wscript.exe 按 ANSI 代码页解析，
       写成 UTF-8 会让中文路径变成乱码，脚本静默失败（和 .bat 是同一个坑）。
    """
    try:
        os.makedirs(STARTUP_DIR, exist_ok=True)
        with open(VBS_FILE, "w", encoding="gbk", errors="replace", newline="") as f:
            f.write(VBS_TEMPLATE % (pyw, WATCHDOG))
        return True
    except Exception as e:
        print("  ✗ 写 VBS 失败: %r" % (e,))
        return False


def del_vbs():
    try:
        if os.path.exists(VBS_FILE):
            os.remove(VBS_FILE)
            return True
    except Exception as e:
        print("  ✗ 删 VBS 失败: %r" % (e,))
    return False


# ---------------- 看门狗进程 ----------------
def watchdog_pid():
    try:
        with open(os.path.join(DIR, "看门狗PID.txt"), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def kill_watchdog():
    pid = watchdog_pid()
    if pid and wbproc.is_alive(pid):
        try:
            subprocess.run([TASKKILL, "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
            time.sleep(1)
            return not wbproc.is_alive(pid)
        except Exception:
            pass
    return False


def start_watchdog():
    """
    立即启动看门狗。

    用 WMI 而不是 Popen —— 如果这个脚本是从 WorkBuddy 的 hook/Bash 里跑的，
    它就在作业对象里，Popen 出来的看门狗会被连坐回收。
    """
    pyw = wbproc.find_pythonw()
    if not pyw:
        print("  ✗ 找不到 pythonw.exe（系统 Python 3.12）")
        return False
    pid = wbproc.launch_via_wmi(pyw, WATCHDOG)
    if pid:
        print("  ✓ 看门狗已启动 pid=%s" % pid)
        return True
    print("  ✗ WMI 启动失败")
    return False


# ---------------- 三个动作 ----------------
def cmd_status():
    pyw = wbproc.find_pythonw()
    print("文件")
    print("  pythonw.exe   : %s" % (pyw or "✗ 找不到"))
    print("  watchdog.pyw    : %s" % ("✓" if os.path.exists(WATCHDOG) else "✗ 缺失"))
    print()
    print("开机自启")
    v = reg_get()
    print("  注册表 Run    : %s" % (v if v else "（未设置）"))
    print("  启动文件夹    : %s" % ("✓ %s" % VBS_FILE if os.path.exists(VBS_FILE) else "（未设置）"))
    banner = os.path.join(DIR, "watchdog_disabled.txt")
    print("  停用标记      : %s" % ("✓ 存在（已主动停用）" if os.path.exists(banner) else "无"))
    print()
    print("运行状态")
    alive = wbproc.watchdog_alive()
    _hb = os.path.join(DIR, "watchdog_heartbeat.txt")
    age = None
    try:
        with open(_hb, "r", encoding="utf-8") as f:
            age = time.time() - float(f.read().strip())
    except Exception:
        pass
    print("  心跳          : %s" % (
        "%.0f 秒前 ✓ 看门狗在跑" % age if age is not None else "（无心跳文件）"))
    print("                 → %s" % ("在管启动" if alive else "不接管（hook 会兜底）"))
    pid = watchdog_pid()
    print("  看门狗 PID    : %s %s" % (pid, "(存活)" if pid and wbproc.is_alive(pid) else "(已退出)"))
    print("  桌宠窗口      : %s" % (wbproc.find_pet_window() or "（未运行）"))
    print("  WorkBuddy 主进程: %s" % (wbproc.main_pid() or "（未运行）"))
    print()
    print("日志尾部（watchdog.log）")
    try:
        with open(os.path.join(DIR, "watchdog.log"), "r", encoding="utf-8",
                  errors="ignore") as f:
            for line in f.readlines()[-6:]:
                print("  " + line.rstrip())
    except Exception:
        print("  （暂无）")


def cmd_install():
    pyw = wbproc.find_pythonw()
    if not pyw:
        print("✗ 找不到 pythonw.exe（系统 Python 3.12），先装好 Python 再来")
        return 1
    if not os.path.exists(WATCHDOG):
        print("✗ 找不到 %s" % WATCHDOG)
        return 1

    print("① 注册表自启")
    reg_set('"%s" "%s"' % (pyw, WATCHDOG))
    got = reg_get()
    print("  %s %s" % ("✓" if got else "✗", got or "写入失败"))

    print("② 启动文件夹自启（VBS）")
    if write_vbs(pyw):
        print("  ✓ %s" % VBS_FILE)

    print("③ 清除停用标记")
    banner = os.path.join(DIR, "watchdog_disabled.txt")
    if os.path.exists(banner):
        try:
            os.remove(banner)
        except Exception:
            pass
        print("  ✓ 已删除")
    else:
        print("  （本来就没有）")

    print("④ 立即启动看门狗")
    if wbproc.watchdog_alive():
        print("  （已经在跑 pid=%s，跳过）" % watchdog_pid())
    else:
        start_watchdog()

    print("⑤ 等 4 秒复核心跳")
    time.sleep(4)
    if wbproc.watchdog_alive():
        pid = watchdog_pid()
        print("  ✓ 心跳新鲜，看门狗在跑（pid=%s）" % pid)
        print()
        print("装好了。现在起：WorkBuddy 一启动，灯 1.5 秒内自动出现；")
        print("           下次开机也会自动生效，不用再管。")
        return 0
    print("  ✗ 心跳没有更新，看门狗可能没起来 —— 看 watchdog.log")
    return 1


def cmd_uninstall():
    print("① 删注册表自启")
    print("  %s" % ("✓ 已删除" if reg_del() else "（本来就没有）"))

    print("② 删启动文件夹 VBS")
    print("  %s" % ("✓ 已删除" if del_vbs() else "（本来就没有）"))

    print("③ 留停用标记（防止 hook.py 又把它补回来）")
    try:
        with open(os.path.join(DIR, "watchdog_disabled.txt"), "w", encoding="utf-8") as f:
            f.write("用户 于 %s 主动停用了看门狗。\n"
                    "想重新启用：python manage_watchdog.py install\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("  ✓ watchdog_disabled.txt")
    except Exception:
        print("  ✗ 写标记失败")

    print("④ 停掉看门狗进程")
    pid = watchdog_pid()
    if kill_watchdog():
        print("  ✓ 已结束 pid=%s" % pid)
    else:
        print("  （没在跑）")

    for f in ("watchdog_heartbeat.txt", "看门狗PID.txt"):
        try:
            os.remove(os.path.join(DIR, f))
        except Exception:
            pass
    print()
    print("卸干净了。桌宠还能用 —— 双击 launch_pet.bat / launch_pet.pyw 手动开，")
    print("但「随 WorkBuddy 自动启停」会退化成只管退出（hook 里还有一层兜底）。")
    return 0


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()
    print("=" * 56)
    print("WorkBuddy 桌宠红绿灯 · 看门狗自启管理")
    print("=" * 56)
    if action == "install":
        return cmd_install()
    if action == "uninstall":
        return cmd_uninstall()
    if action in ("status", "check", "st"):
        cmd_status()
        print()
        print("可用动作：status（默认） / install / uninstall")
        return 0
    print("未知动作：%s" % action)
    print("可用动作：status（默认） / install / uninstall")
    return 1


if __name__ == "__main__":
    sys.exit(main())
