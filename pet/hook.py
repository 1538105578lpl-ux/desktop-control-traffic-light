#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 桌宠红绿灯 —— Hook 信号端

职责（四件）：
  1. 接收 WorkBuddy 的 hook 事件 JSON（从 stdin），翻译成灯的状态，原子写入 state.json
  2. 每次运行都沿父进程链找回 WorkBuddy 主进程 PID，写进 wb_pid.txt，
     给桌宠做「WorkBuddy 彻底退出就自己关掉」的判据
  3. 确保**看门狗**在跑（watchdog.pyw）—— 它才是"打开 WorkBuddy 灯就自动出现"的主链路，
     开机自启、1.5 秒轮询，与 hook 触发时机完全无关。这里只是兜底自愈
  4. 看门狗没在管时（首次安装 / 已停用），由 hook 亲自把桌宠拉起来

🚦 启动链路（2026-09-19 重构，用户 报障「重启后灯不出现」）：
     主：开机自启看门狗 ──1.5s 轮询──► WorkBuddy 在跑 且 灯不在 → 拉起灯
     备：hook 事件 ──► ensure_watchdog_running() + ensure_pet_running()
   为什么不让 hook 独挑大梁：实测 WorkBuddy 启动时那条链路要 5 秒
   （Setup → WMI/PowerShell 冷启动 1s → 进程起 → 窗口可枚举 3s），
   而且万一 Setup 那次没触发，灯就永远不出现、没有任何兜底。

设计原则：
  1. 绝不阻塞 WorkBuddy —— 任何异常都被吞掉，永远 exit 0
     （看门狗在管时**绝不调 WMI**，那会白白多花 1 秒拖慢 WorkBuddy 启动）
  2. 原子写入（tmp + os.replace），避免桌宠读到半个文件
  3. 只处理"会改变灯色"的事件，忽略高频的 PreToolUse/PostToolUse（省 IO）

灯色语义（用户 2026-09-16 重定义）：
  working  红灯  有任务在跑（不管 1 个还是 N 个）
  waiting  黄灯  **停下来了、在等 用户** —— 只有两种情形：
                  ① 需要 用户 做选择（真弹了权限框 / MCP 要输入）
                  ② 任务暂停（空闲等输入 / 长时间无动静）
  done     绿灯  全部干完了
  error    橙闪  轮次因 API 错误结束
  idle     熄灯  会话开始或结束

🔴 血的教训（2026-09-16，用户 亲测报障）：
  PermissionRequest **绝不能**点亮黄灯。它在正常干活时**每次工具调用都发**
  （实测 2 分钟内 16 次、每 8 秒一对，而 用户 那段时间根本没被问到任何东西）。
  官方文档 plugins-reference.md 说它"权限对话框出现时"，**与实测不符** —— 以实测为准。
  PermissionDenied 同理：官方定义是"被**自动模式分类器**拒绝"，是机器自动判的，
  压根不需要 用户 出手，点黄灯也是误报。

  真正的"等你"信号只有两个：
    · Notification(permission_prompt)  真弹了权限框
    · Elicitation                      MCP 工具向 用户 要输入

🔴 二次血的教训（2026-09-16 上午，用户 再次报障"没任务在跑为什么黄灯"）：
  Notification(idle_prompt) 也**绝不能**点黄灯 —— 它在 WorkBuddy 源码里就是一个
  裸的 60 秒 setTimeout：

      this.resetIdleTimer(eA);
      await executeNotificationHooks(eA, "CodeBuddy is waiting for your input",
                                     NotificationType.IDLE_PROMPT)
      ...
      , 6e4);          // ← 60000ms

  也就是说：**任何一轮 Stop 之后，只要 60 秒没有新事件，它必然触发**。
  实测铁证：09:11:07 Stop → 09:12:07 idle_prompt；09:18:42 Stop → 09:19:42 idle_prompt，
  分秒不差。它表达的是"这一轮完了、用户 还没说话"，跟"需要 用户 决策"毫无关系，
  而且会把刚点亮的**绿灯覆盖成黄灯** —— 正是 用户 看到的现象。

  permission_prompt 则是**可靠**的：源码里它只在真的弹出审批框时才发 ——
      log(`Approval dialog shown for tool: ${el}, ..., waiting for user response...`);
      executeNotificationHooks(eA, `needs your permission to use ${el}`,
                               NotificationType.PERMISSION_PROMPT)
  而且 message 里带工具名，看日志能直接知道它在等哪个工具。
"""

import json
import os
import subprocess
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
if DIR not in sys.path:
    sys.path.insert(0, DIR)          # 保证能 import 同目录的 wbproc.py

import wbproc                        # noqa: E402

STATE_FILE = os.path.join(DIR, "state.json")
TRAFFIC_LIGHT = os.path.join(DIR, "traffic_light.py")
LOG_FILE = os.path.join(DIR, "event_log.log")

# 直接映射：事件名 → 状态
EVENT_STATE = {
    "UserPromptSubmit": "working",      # 你按下发送
    "PermissionRequest": "working",     # ← 改自 waiting。高频=工具正在跑，是"干活"不是"等你"
    "ElicitationResult": "working",     # 你答完了 MCP 的提问 → 我接着干
    "Elicitation": "waiting",           # MCP 工具向你要输入（真的在等你）
    "Stop": "done",                     # 主代理这一轮答完了
    "StopFailure": "error",             # 轮次因 API 错误结束
    "Setup": "idle",                    # 应用/会话启动（WorkBuddy 启动流程里的第一个事件）
    "SessionStart": "idle",
    "SessionEnd": "idle",
}

# Notification 里**只有** permission_prompt 算"在等你"（源码：真弹了审批框才会发）
# idle_prompt 是 60 秒空闲定时器，语义是"这一轮完了、用户 还没说话"，点黄灯是误报
WAITING_NOTIFICATIONS = {"permission_prompt"}

# 看门狗脚本（开机自启的主入口，见 watchdog.pyw 顶部注释）
WATCHDOG_PYW = os.path.join(DIR, "watchdog.pyw")

# 记录"最近一次尝试启动桌宠"的时间戳，避免窗口还没建出来时被连续重复拉起
LAUNCH_STAMP = os.path.join(DIR, "pet_launch.txt")
LAUNCH_COOLDOWN = 15          # 秒

# 详细诊断日志（只记启动相关，量小）
DIAG_FILE = os.path.join(DIR, "startup_diag.log")

# 拉起桌宠时的进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# ⚠️ SUPPRESS_FILE（pet_suppressed.txt）/ find_pythonw / launch_via_wmi
#    已下沉到 wbproc.py —— 因为看门狗也要用同一份逻辑，
#    两处各写一份必然会打架（你关掉灯、另一条链路立刻又拉起来）。


# ---------------- 排查用的小日志（滚动保留最近 100 行） ----------------
def log_event(data, payload=None):
    """
    记一行事件日志。格式：
      2026-09-16 08:44:42  PermissionRequest  -> working  anchor=17104
      2026-09-16 08:47:23  Notification(permission_prompt) -> waiting  anchor=17104

    为什么要把映射结果也写进去：以后黄灯再出问题，一眼就能看出
    "是哪个事件把它点黄的"，不用再去猜。
    """
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 64 * 1024:
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-100:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(tail)
        event = (data.get("hook_event_name") or "-")
        # Notification 必须带上类型，否则没法区分"真弹框"和"空闲提醒"
        if event == "Notification":
            event = "Notification(%s)" % (data.get("notification_type") or "-")
        state = (payload or {}).get("state") or "(不动灯)"
        # message 很有用：permission_prompt 的 message 形如
        #   "needs your permission to use Bash"
        # —— 一眼看出它在等哪个工具，避免下次又靠猜
        msg = (data.get("message") or "").strip().replace("\n", " ")[:56]
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s\t%-32s -> %-8s anchor=%-6s %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                event,
                state,
                wbproc.read_anchor() or "-",
                ('"%s"' % msg) if msg else "",
            ))
    except Exception:
        pass


# ---------------- 启动诊断日志（滚动保留最近 60 行） ----------------
def diag(msg):
    try:
        if os.path.exists(DIAG_FILE) and os.path.getsize(DIAG_FILE) > 32 * 1024:
            with open(DIAG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-60:]
            with open(DIAG_FILE, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(DIAG_FILE, "a", encoding="utf-8") as f:
            f.write("%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------- 把桌宠拉起来 ----------------
def _launch_recently():
    """15 秒内刚尝试过启动 → 先别急着重来（窗口建立需要一两秒）。"""
    try:
        with open(LAUNCH_STAMP, "r", encoding="utf-8") as f:
            return (time.time() - float(f.read().strip())) < LAUNCH_COOLDOWN
    except Exception:
        return False


def _mark_launch():
    try:
        with open(LAUNCH_STAMP, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def ensure_watchdog_running():
    """
    看门狗没在跑就补一个。返回 True 表示「本次真的把看门狗启动了」。

    正常情况它是**开机自启**的（注册表 + 启动文件夹双保险），走不到这里。
    这一步纯粹是兜底：万一自启项被安全软件清了、或看门狗被误杀，
    下一条消息就能把它补回来 —— 「自动拉起桌宠」这条链路永不掉。

    ⚠️ 用户 主动卸载过（watchdog_disabled.txt 存在）→ 必须尊重，绝不再私自拉起。
    """
    try:
        if wbproc.watchdog_disabled() or wbproc.watchdog_alive():
            return False
        if not os.path.exists(WATCHDOG_PYW):
            return False
        pyw = wbproc.find_pythonw()
        if not pyw:
            return False
        pid = wbproc.launch_via_wmi(pyw, WATCHDOG_PYW)
        diag("看门狗不在 → 已补起 pid=%s" % (pid or "失败"))
        return bool(pid)
    except Exception:
        return False


def ensure_pet_running():
    """
    桌宠没在跑就拉起来；已经在跑就什么都不做。

    正常路径极便宜（一次 FindWindowW），所以每个 hook 事件都能调，
    桌宠万一被关掉，下一条消息就自愈。
    只有真的需要启动时才会走 WMI（约 1~2 秒）。

    🔴 2026-09-19 起：**看门狗在管就直接放行**。
       原来这里会在 WorkBuddy 启动时同步调 WMI，阻塞约 1 秒（PowerShell 冷启动），
       白白拖慢 WorkBuddy 启动，而且整条链路要 5 秒灯才出得来。
       现在启动交给看门狗（1.5 秒轮询、独立于 hook），hook 只管灯色。
    """
    try:
        if wbproc.find_pet_window():
            return
        if wbproc.user_suppressed():
            return
        if wbproc.watchdog_alive():
            return                      # 看门狗在管，别抢它的活，也别阻塞 WorkBuddy
        if _launch_recently():
            return

        pyw = wbproc.find_pythonw()
        if not pyw or not os.path.exists(TRAFFIC_LIGHT):
            diag("放弃启动: pythonw=%s traffic_light=%s" % (
                pyw, os.path.exists(TRAFFIC_LIGHT)))
            return

        _mark_launch()

        # 主力：WMI（唯一能逃出作业对象的方式）
        pid = wbproc.launch_via_wmi(pyw, TRAFFIC_LIGHT)
        if pid:
            diag("已通过 WMI 启动桌宠 pid=%s" % pid)
            return

        # 兜底：直接 Popen。逃不出作业对象，但至少同一次调用内是活的，
        # 且下次事件会重试；万一哪天 WorkBuddy 不再用作业对象，它就成了。
        try:
            subprocess.Popen(
                [pyw, TRAFFIC_LIGHT],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            diag("WMI 不可用，退化为普通 Popen（可能被作业对象回收）")
        except Exception as e:
            diag("Popen 也失败: %r" % (e,))
    except Exception as e:
        diag("ensure_pet_running 异常: %r" % (e,))


# ---------------- 状态负载 ----------------
def build_payload(data):
    """把 hook 输入翻译成状态负载；不需要处理的返回 None。"""
    event = (data.get("hook_event_name") or "").strip()

    if event == "Notification":
        ntype = (data.get("notification_type") or "").strip()
        if ntype not in WAITING_NOTIFICATIONS:
            return None
        state = "waiting"
    else:
        state = EVENT_STATE.get(event)
        if state is None:
            return None

    payload = {
        "state": state,
        "event": event,
        "ts": time.time(),
        "session_id": data.get("session_id") or "",
        "cwd": data.get("cwd") or "",
    }

    # 你刚发的指令内容，窗口上可以拿来显示"在忙什么"
    if event == "UserPromptSubmit":
        payload["detail"] = (data.get("prompt") or "").strip()[:60]

    return payload


def write_state(payload):
    """原子写入，避免桌宠读到写了一半的文件。"""
    from state_store import update_state
    update_state(STATE_FILE, payload)


def main():
    raw = ""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "ignore")
    except Exception:
        return
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return

    # 1. 先锚定 WorkBuddy 主进程（便宜：只在父链上走几步）
    try:
        wbproc.anchor_from_ancestors()
    except Exception:
        pass

    # 2. 翻译成灯的状态
    payload = build_payload(data)
    if payload:
        write_state(payload)

    # Trusted human task events also feed the input gate; never start a service here.
    try:
        from state_store import record_hook
        from desktop_bridge import HOOK_RECORDS
        record_hook(HOOK_RECORDS, data)
    except Exception:
        pass

    # 3. 记一笔排查日志（含事件名、通知类型、映射结果）
    log_event(data, payload)

    # 4. 确保看门狗在跑 —— 它是"打开 WorkBuddy 灯就自动出现"的主链路
    #    （开机自启 + 1.5 秒轮询，与 hook 时机完全无关）。
    #    这一步只是兜底自愈：自启项被清了、看门狗被误杀了，这里把它补回来。
    started_watchdog = ensure_watchdog_running()

    # 5. 确保桌宠在跑 —— 看门狗在管的话这一步立刻返回，不会用 WMI 阻塞 WorkBuddy。
    #    看门狗没在管（首次安装、或已停用）时才由 hook 亲自出手。
    #
    # 🔴 刚把看门狗拉起来时，这里必须**直接放行**：
    #    看门狗 1.5 秒内就会自己拉起灯，再用 WMI 串行等一遍纯属白等
    #    （实测那样会让 WorkBuddy 启动多卡 1.5 秒：两次 WMI 串行共 3.2 秒）。
    if started_watchdog:
        return
    ensure_pet_running()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 静默失败：hook 报错会影响 WorkBuddy，宁可灯不亮
        pass
    sys.exit(0)
