#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把桌宠红绿灯的 hooks 写进 ~/.workbuddy/settings.json（先备份）。"""
import datetime
import json
import os
import shutil
import sys
from pathlib import Path

WB_HOME = Path(os.environ.get("WORKBUDDY_HOME") or (Path.home() / ".workbuddy"))
SETTINGS = str(WB_HOME / "settings.json")
BK_DIR = str(WB_HOME / "backups")
PY = os.environ.get("PET_PYTHON") or sys.executable
HOOK = str(Path(__file__).resolve().with_name("hook.py"))
CMD = '"%s" "%s"' % (PY, HOOK)


def block():
    return [{"hooks": [{"type": "command", "command": CMD, "timeout": 10}]}]


inner = [{"type": "command", "command": CMD, "timeout": 10}]

HOOKS = {
    # Setup(init) 是 WorkBuddy 启动流程里的**第一个** hook 事件，
    # 比 SessionStart 还早 → 用它来"打开应用就把桌宠拉起来"最靠谱
    "Setup": [{"matcher": "init", "hooks": inner}],
    "SessionStart": block(),
    "UserPromptSubmit": block(),
    # PermissionRequest 只用来点亮**红灯**（它高频 = 工具在跑）。
    # 🔴 不要再拿它点黄灯，2026-09-16 已踩过坑（详见 hook.py 顶部注释）
    "PermissionRequest": [{"hooks": inner}],
    # Elicitation / ElicitationResult：MCP 向 用户 要输入 → 黄灯；用户 答完 → 回红灯
    "Elicitation": [{"hooks": inner}],
    "ElicitationResult": [{"hooks": inner}],
    # 🔴 只有 permission_prompt 才是"真的在等 用户"（源码：真弹出审批框才会发）。
    # idle_prompt 仍然注册，但**只当探针**：hook.py 收下后不点灯（返回 None），
    # 只在 `event_log.log` 留一行 `-> (不动灯)`。留着它是因为它是"这一轮结束后
    # 空闲了 60 秒"的唯一可观测痕迹，排查「灯为什么不动了」时有用。
    # ⚠️ 千万别再把它接回黄灯 —— 它是 60 秒 setTimeout，会把绿灯覆盖成黄灯，
    #    2026-09-16 上午 用户 报障"没任务在跑为什么黄灯"就是这个（详见 hook.py 顶部注释）。
    "Notification": [
        {"matcher": "permission_prompt", "hooks": inner},
        {"matcher": "idle_prompt", "hooks": inner},
    ],
    "Stop": block(),
    "StopFailure": block(),
    "SessionEnd": block(),
}

if not os.path.exists(SETTINGS):
    raise SystemExit("找不到 %s，请先运行一次 WorkBuddy 生成配置文件。" % SETTINGS)

raw = open(SETTINGS, "rb").read()
has_bom = raw.startswith(b"\xef\xbb\xbf")
cfg = json.loads(raw.decode("utf-8-sig"))

os.makedirs(BK_DIR, exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
bk = os.path.join(BK_DIR, "settings_backup_%s.json" % stamp)
shutil.copy2(SETTINGS, bk)


def is_ours(item):
    """这条 hook 是不是桌宠自己注册的（命令完全一致）。"""
    return any((h.get("command") or "").strip() == CMD
               for h in (item.get("hooks") or []))


# ⚠️ 只增量合并，不做全量覆盖。
# 之前是 cfg["hooks"] = HOOKS —— 等于把 用户 别的工具/插件注册的 hooks 全删了，
# 是个隐患（2026-09-16 修）。现在：
#   · 我们接管的事件：保留别人的条目，再接上我们的
#   · 别的事件：原样保留
#   · 我们**不再**接管的事件（比如已废弃的 PermissionDenied）：只摘掉自己的条目，
#     如果那个事件下再没别人，就把空事件一起删掉
old_hooks = dict(cfg.get("hooks") or {})
merged = {}
replaced = []
retired = []
for ev, arr in old_hooks.items():
    keep = [it for it in (arr or []) if not is_ours(it)]
    had_ours = len(keep) != len(arr or [])
    if ev in HOOKS:
        if had_ours:
            replaced.append(ev)
        merged[ev] = keep + HOOKS[ev]
    elif keep:
        merged[ev] = keep                 # 不是我们的事件、还有别人的条目 → 原样留着
    elif had_ours:
        retired.append(ev)                # 只剩我们的旧条目 → 整个删掉
for ev in HOOKS:
    if ev not in merged:
        merged[ev] = HOOKS[ev]
cfg["hooks"] = merged

with open(SETTINGS, "w", encoding="utf-8-sig" if has_bom else "utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)

print("备份文件 :", bk)
print("原有 hooks:", list(old_hooks.keys()) if old_hooks else "（无）")
print("写入事件 :", ", ".join(HOOKS.keys()))
print("hook 命令:", CMD)
print("已接管  :", ", ".join(replaced) if replaced else "（无，首次安装）")
print("已摘除  :", ", ".join(retired) if retired else "（无）")
print()
chk = json.load(open(SETTINGS, encoding="utf-8-sig"))
print("复核 -> settings.json 顶层键:", list(chk.keys()))
print("复核 -> hooks 事件数:", len(chk["hooks"]), "->", ", ".join(chk["hooks"].keys()))
