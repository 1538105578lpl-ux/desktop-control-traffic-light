# 架构说明

## 1. 一次完整的电脑任务

```
你：在钉钉里给张三发一句"下午三点开会"
 │
 ├─ 客户端把这句话作为 UserPromptSubmit hook 事件发给 pet/hook.py
 │     └─ hook.py 原子写入 state.json → {"state":"working", ...}
 │     └─ traffic_light.py 200ms 轮询读到 → 点红灯，显示"干活中"
 │
 ├─ AI 决定用电脑工具，先调 BeginDesktopTask(title="给张三发消息")
 │     └─ panel_gateway 校验 token → panel_service 登记任务
 │     └─ 读取真实 UserPromptSubmit 事件，把任务绑定到当前会话
 │     └─ 校验独占锁：无其他任务占用 → 拿到所有权
 │
 ├─ AI 调 WindowList / FocusWindow 自己定位钉钉（不需要你选窗口）
 ├─ AI 调 Snapshot 观察 → 拿控件树 + 坐标
 ├─ AI 调 Click / Type 执行
 │     └─ 每次输入：检查暂停锁 → 检查观察是否过期 → 通过 input_guard 注入
 │     └─ 输入后当次观察作废，必须重新 Snapshot 才能再操作
 │
 ├─ AI 调 TaskProgress("已定位到张三的会话")
 │     └─ 红绿灯展开时显示这一步文案
 │
 ├─ AI 调 EndDesktopTask(status="ended")
 │     └─ 释放独占锁，任务标记结束
 │
 └─ 客户端发 Stop hook → hook.py 写 state=done → 绿灯
```

**关键点**：红绿灯只是这条链路的一个观察者 + 干预入口，不参与决策。
即使灯挂了、服务挂了，任务也只是失去可视化，不会执行错误的操作。

## 2. 三个状态各自存在哪

| 状态 | 存放 | 写入方 | 读取方 |
|---|---|---|---|
| 灯色 / 当前轮次 | `pet/state.json` | `hook.py` | `traffic_light.py` |
| 暂停锁 / 任务所有权 | `server/panel-runtime/control-intent.json` | `panel_service.py` | gateway、input_guard |
| 本地 token / 端口 | `server/panel-runtime/connection.json` | `panel_service.py` 启动时 | 桌宠、gateway |

三者都是**文件落盘 + 原子替换**（tmp + `os.replace`），不做内存态：
服务被重启、桌宠被关掉，状态不丢。

## 3. 暂停状态机

```
             ┌──────────────── 暂停按钮 / Ctrl+Alt+F12 / 接管 ─────────────┐
             ▼                                                            │
      ┌─────────────┐                                              ┌──────┴──────┐
      │    RUNNING  │ ◄──「原任务里明确说继续」+ BeginDesktopTask ── │   PAUSED    │
      └─────────────┘                                              └─────────────┘
             │                                                            ▲
             │ 任务结束 / 异常                                            │ 服务重启后恢复暂停意图
             ▼                                                            │
      ┌─────────────┐                                                     │
      │    IDLE     │ ────────────────────────────────────────────────────┘
      └─────────────┘
```

- `PAUSED` 是**锁存**状态：写盘，重启后依然暂停
- 解除时强制要求"同一会话 + 人类明确指令"，两者缺一不可
- 解除后强制 `Snapshot` 一次：因为暂停期间你可能手动改过界面，旧坐标全废

## 4. 独占锁

`control-intent.json` 里的 `owner_session` 记录当前持有电脑操作权的会话。

- 第二个任务调 `BeginDesktopTask` 时拿到 `CONTROL_BUSY`，**不会**抢锁
- 但第二个任务仍可正常做非电脑工作（写文档、查资料、跑脚本）
- 这也是为什么红绿灯文案强调「电脑操作已暂停」，而不是「整个 AI 已停止」——
  说"全部停止"是假的，会误导你

## 5. 观察时效（最容易出错的地方）

坐标是有保质期的。窗口移动、切会话、弹窗、任何输入之后，一次 `Snapshot` 拿到的坐标随时失效。

规则：

```
FocusWindow → Click        （两次调用之间不要插 Snapshot）
Snapshot   → Click         （坐标必须来自最近一次 Snapshot）
Any Input  → Snapshot 失效 （必须重新观察）
```

漏了这一条最典型的后果：**点击静默落到别的窗口上，且没有任何报错**。
实现上由 `server/reliability.py` 给每次观察打时间戳，`input_guard` 拒绝使用过期观察。

## 6. 为什么把 vendor 整包放进来

三个现实原因：

1. 本项目的修复散落在 15 个文件里，用 patch 分发对使用者太不友好
2. 上游 0.8.8 的部分模块（接管层、overlay）与暂停锁直接冲突，装新版本会坏
3. 基线固定在 0.8.5 后，"什么版本能跑"是确定的，issue 可复现

代价是同步上游变麻烦。如果你要升级基线，请对照 `docs/UPSTREAM-DIFF.md` 逐个迁移，
不要直接 `pip install -U windows-mcp`。
