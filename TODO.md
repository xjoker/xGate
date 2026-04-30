# TODO

未排期 / 待规划的工作项。按优先级排列。

---

## P0

（暂无）

## P1

（暂无 — 原 HttpOnly Cookie 鉴权方案 C 已落地，见 CHANGELOG Unreleased）

## P2

- 引入 `tiktoken` 做更精确的 token 估算（当前 `len(text)//4` 仅做兜底）
- `service_tier` / `metadata` / `store` 等 OpenAI 字段做实际持久化（目前仅校验吞掉）
- 真实支持 `tools` / `tool_choice`（function calling），需要在 chat 模板里注入
- session token 持久化到 SQLite（当前内存 dict，进程重启即失效）

## MCP 后续增强

- **OAuth 2.1**：替代当前 Bearer 静态 API Key，支持 MCP 官方 OAuth 2.1 授权流程
- **流式 tool 输出**：MCP 2025-06-18 spec 已定义 `tools/stream`，待主流客户端跟进后接入
- **官方 api.x.ai 通道兜底**：当网页 cookie 失效时自动切换到 xAI 官方 API（需用户提供 xAI API Key）
- **MCP session 持久化**：将 `mcp_session` 的 `(session_id → conversation_id, response_id)` 映射持久化到 SQLite，进程重启后续轮状态不丢失
- **全局并发限流**：单 cookie 单账号配额有限，MCP 多客户端并发时加令牌桶限流，避免快速耗尽
