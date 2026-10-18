# 安全模型 / Security Model

这个项目的本质是**给一个能操作你键鼠的程序加外壳**，所以安全问题必须讲清楚，
而不是用「本地运行所以安全」一句带过。

## 1. 信任边界

| 组件 | 信任级别 | 说明 |
|---|---|---|
| `server/panel_service.py` | 高 | 有完整键鼠控制权，只信任带正确 token 的本地请求 |
| `pet/traffic_light.py` | 中 | 只显示状态、发暂停/接管/预览请求，不能执行输入 |
| `vendor/windows_mcp/` | 中 | 实际执行层，被 gateway 约束可用工具集 |
| 调用的 AI 客户端 | **低** | 它给出的每个操作都要能被打断、被审计 |

**核心假设：AI 是不可信的**。因此所有「停止」都必须在**服务侧**强制执行，
不能依赖 AI 自觉不调用工具——AI 忘了、理解错了、或者换了个工具绕过去都是常态。

## 2. 网络面

- 监听地址硬编码 `127.0.0.1:18765`，不绑 `0.0.0.0`
- 不接受任意外部连接，不做端口转发
- 除上游可选遥测外（默认关闭），不发起任何出网请求

威胁模型外的情形：**你的机器已被入侵**。此时本地回环不构成防护，任何方案都不构成。

## 3. 鉴权

每次服务启动生成一对随机凭据：

```python
TOKEN    = secrets.token_urlsafe(32)
INSTANCE = secrets.token_hex(12)
```

- 写入 `server/panel-runtime/connection.json`（**已在 `.gitignore` 中**）
- 进程退出即失效，不持久化、不可跨重启复用
- 所有 API 请求必须带 `X-Control-Token` 头，用 `secrets.compare_digest` 做常量时间比较
- 附带 `Sec-Fetch-Site: same-origin` + `Referer` 校验，拦截浏览器跨站发起

> 这条设计针对的真实风险：本机任意网页都能向 `127.0.0.1:18765` 发 POST
> （DNS rebinding / CSRF 到 localhost）。没有 token 和来源校验，一个恶意网页就能替你点确认。

## 4. 暂停 = 锁定状态

这是整个项目最关键的契约：

```
暂停后：
  ├─ 后续所有电脑输入被拒绝（CONTROL_PAUSED / CONTROL_INTERRUPTED）
  ├─ 已获取的观察（Snapshot/Screenshot）全部作废（STALE_ACTION）
  ├─ 排队中的输入失效
  ├─ 本服务按住的按键立即释放（防止 Ctrl/Shift 卡住）
  └─ 暂停意图落盘，服务重启后依然保持
```

解除条件**只有一个**：原任务里由人类明确说出「继续」「继续刚才的任务」「恢复电脑操作」。

以下**都不算**：普通闲聊、点开详情、点「回任务」、重连连接器、重启服务、AI 自己判断"差不多该继续了"。

## 5. 能力收窄

相对上游被移除的能力：

| 移除项 | 原因 |
|---|---|
| 任意 PowerShell 执行 | 等于无条件远程代码执行 |
| 任意可执行文件启动（`App(mode="launch_executable")`） | 同上，且无法审计 |
| 浏览器登录接口 / Web 操作页 | 减少暴露面，操作入口只留 MCP |

保留但仍需注意的能力：

- `App(mode="switch")` 可切换/恢复任意窗口 → 有前台窗口校验，1 秒内验证是否真的到前台
- `Type` 可向任意获得焦点的控件输入 → 输入前必须有当次 `Snapshot`，坐标有时效
- `Clipboard` 读写 → 结束后恢复原剪贴板内容，但**不覆盖用户在此期间写入的新值**

## 6. 数据留存

| 数据 | 位置 | 是否提交到仓库 |
|---|---|---|
| 会话 ID、任务标题、步骤文案 | `pet/state.json` | ❌ 已 gitignore |
| hook 原始事件（含提示词全文） | `server/panel-runtime/hook-events.json` | ❌ 已 gitignore |
| 本地 token | `server/panel-runtime/connection.json` | ❌ 已 gitignore |
| 诊断日志 | `pet/*.log`、`server/panel-runtime/*.log` | ❌ 已 gitignore |
| 截图 | 内存中，仅在预览展开时按需获取 | ❌ 不落盘 |

**如果你要提 issue 附日志**：先自己过一遍，日志里可能有你的任务文案、窗口标题、
文件路径、聊天对象昵称。建议手工打码后再贴。

**如果你要提 PR**：请先跑一次

```powershell
git status --ignored        # 确认没有运行时文件被 add
git grep -nEi "token|password|secret|C:\\\\Users" -- "*.py" "*.md"
```

## 7. 供应链

- `vendor/windows_mcp/` 是上游源码快照，基线 **0.8.5**，已记录改动清单（`docs/UPSTREAM-DIFF.md`）
- `requirements.lock` 锁定精确版本，可复现安装
- 上游遥测（PostHog）在 `server/launcher.py` 中强制 `ANONYMIZED_TELEMETRY=false`

发现安全问题请**不要开公开 issue**，用仓库的 Security 选项卡私下报告。
