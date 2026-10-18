# 与上游的差异 / Upstream Diff

`vendor/windows_mcp/` 基线：**CursorTouch/Windows-MCP 0.8.5**（MIT）。
本仓库在该基线上做了两类改动：**从上游 0.8.6–0.8.8 移植的修复** + **本项目的红绿灯接入与安全收窄**。

改法是**选择性移植**，不整体替换 0.8.8 —— 因为上游新版本引入了自己的接管/覆盖层模块，
会和本项目的暂停机制形成双轨冲突。

---

## 一、从 0.8.6 – 0.8.8 移植的修复

| 文件 | 改动 | 解决什么 |
|---|---|---|
| `uia/core.py` | 整文件更新，使用多显示器虚拟桌面坐标（`VIRTUALDESK`） | 多屏下点击坐标偏移 |
| `desktop/utils.py` | 新增 `as_bool`、`repair_surrogates` | 布尔参数解析、非法代理字符导致崩溃 |
| `desktop/screenshot.py` | 静默快照后端降级链 | 某截图后端不可用时整次观察失败 |
| `infrastructure/thread_affinity.py` | **新增** | UIA 调用绑定线程的 COM apartment，避免跨线程写 |
| `infrastructure/eventloop.py` | **新增** | 事件循环辅助（self-pipe 守护，见下方限制） |
| `tree/cache_utils.py` | 更新 | 控件树缓存失效策略 |
| `tree/config.py` | 补 `SliderControl` 映射 | 滑块控件被识别成未知类型 |
| `tree/views.py` | 滑块值 `0` 与 `range` 正确显示 | 值为 0 时被当成"无值" |
| `tree/service.py` | 祖先 `RuntimeId` 防循环、死 UIA 子树跳过、支持排除进程 | 深树遍历死循环、访问已销毁控件卡死 |
| `infrastructure/analytics.py` | 新增 `run_sync_in_thread`、`run_uia_affine` | 同步调用阻塞事件循环 |
| `infrastructure/__init__.py` | 导出上述辅助 | — |
| `tools/input.py` | 采用上游版输入实现 + 重新应用本项目补丁 | 输入延迟、焦点归属 |
| `tools/snapshot.py` | `Snapshot` / `Screenshot` 走 UIA 专用线程 | 截图时阻塞其他工具 |
| `tools/_snapshot_helpers.py` | 接入 `repair_surrogates` | 同上 |
| `desktop/service.py` | `switch_app` 前台校验（1 秒）、`_validate_screen_point` 越界拦截、负数 `label` 报错、剪贴板恢复不覆盖用户新值 | 点了没反应还无报错、坐标越界静默失败 |

## 二、本项目自有的增量

| 文件/模块 | 内容 |
|---|---|
| `server/task_lifecycle.py` | `BeginDesktopTask` / `TaskProgress` / `EndDesktopTask` 三件套与状态机 |
| `server/control_state.py` | 暂停锁（落盘、重启保持）、独占锁、所有者会话 |
| `server/input_guard.py` | 输入拦截：暂停期间拒绝一切键鼠注入 |
| `server/reliability.py` | 观察时效（旧坐标作废）、按键释放、失败重试 |
| `server/panel_service.py` | FastAPI 服务 + 26 工具的统一后端 |
| `server/panel_gateway.py` | token 鉴权 + 同源校验 + 路由 |
| `server/panel_hotkey.py` | `Ctrl+Alt+F12` 全局暂停热键 |
| 工具层新增 | `ControlStatus`、`WindowList`、`FocusWindow`、`ReadFocusedInput`；`Type` 返回结构化校验结果 |
| 安全收窄 | 不暴露任意 PowerShell、不暴露任意可执行启动 |

## 三、刻意**未**采用的上游 0.8.8 模块

| 上游模块 | 不采用的原因 |
|---|---|
| `control*.py`（接管层） | 与本项目暂停锁职责重叠，双轨会互相覆盖状态 |
| `overlay_bitmap` / `flash_overlay` | 与桌宠红绿灯的视觉反馈冲突 |
| `tools/control_status.py` / `tools/control_notifications.py` | 由本项目的 `ControlStatus` 取代 |
| `watchdog/` （上游进程守护） | 本项目用 `pet/watchdog.pyw` 的看门狗链路 |
| `uia/controls.py` | ARM64 专用，x64 环境下无意义 |
| `infrastructure/security.py` 的扩展部分 | 本项目使用自己的 token + 同源校验 |

## 四、已验证的环境限制

**`install_selfpipe_guard` 在 Windows 默认事件循环下无效。**
本机默认是 `ProactorEventLoop`，而上游该函数只 patch `BaseSelectorEventLoop`。
因此 `infrastructure/eventloop.py` 被保留但**未接入 launcher**，不宣称已修复该问题。

如果你在 `SelectorEventLoop` 下运行，可以自行接入。

## 五、重新应用补丁的方式

`tools/input.py` 等文件采用「以上游为准 + 重新打本项目补丁」的方式维护。
补丁脚本在 `server/` 同级未随仓库发布（涉及本机路径），
如果你需要扩展这块，建议直接把差异写成显式的 patch 文件，避免下次同步上游时丢失。
