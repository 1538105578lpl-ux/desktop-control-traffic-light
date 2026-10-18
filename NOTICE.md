# 第三方组件声明 / Third-Party Notices

本仓库包含第三方开源代码，其版权归原作者所有，并按各自许可证分发。

## CursorTouch / Windows-MCP

- 上游项目：https://github.com/CursorTouch/Windows-MCP
- 许可证：MIT
- 版权：Copyright (c) CursorTouch
- 使用位置：`vendor/windows_mcp/`（整包源码，基线版本 **0.8.5**）

`vendor/windows_mcp/` 并非原样拷贝，其中包含本项目作者所做的修改，详见
`docs/UPSTREAM-DIFF.md`。改动同样以 MIT 许可证发布，但**不改变上游代码的原始版权归属**。

上游许可证全文：

```
MIT License

Copyright (c) CursorTouch

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 隐私与遥测说明

上游 `windows_mcp/infrastructure/analytics.py` 包含基于 PostHog 的匿名使用统计逻辑，
本项目**默认关闭**：`server/launcher.py` 启动前会设置 `ANONYMIZED_TELEMETRY=false`。
若你自行移除该设置，遥测行为由上游代码决定，与本项目无关。

本仓库不含任何用户数据、会话记录、凭据或本地配置。仓库中所有路径均通过
`Path(__file__)`、`%LOCALAPPDATA%`、`%USERPROFILE%` 或环境变量解析。
