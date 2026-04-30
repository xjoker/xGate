# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **MCP Streamable HTTP 接入**（`/mcp`）：将 Grok 网页能力暴露为 9 个 MCP tool，
  兼容 Claude Desktop / Cursor / Cline 等所有支持 MCP 2025-06-18 spec 的客户端
  - `grok_chat`：多轮对话，支持自动续轮、富格式 / OpenAI 兼容双输出、X/web 搜索结果内嵌
  - `grok_x_search`：X 高级搜索（含时间范围、最小互动数、媒体类型等 15+ 过滤参数），返回原始推文结构
  - `grok_web_search`：Web 搜索，返回 url / title / preview 结构化结果
  - `grok_quota`：查询当前账号在指定模型的剩余配额
  - `grok_imagine`：图片生成（chat 通道），支持 url / local_path / base64 三种返回模式
  - `grok_imagine_video`：视频生成，下载后本地缓存，返回代理 URL 或本地路径
  - `grok_files_list` / `grok_files_save_local` / `grok_files_delete`：云端 Grok Files 管理
- `/v1/files/proxy`：带鉴权的 `assets.grok.com` 代理端点，供 MCP 客户端无 Cookie 访问生成图片
- `mcp_session`：内存 session store，同一 MCP session 内 `grok_chat` 自动续轮
- `[mcp]` TOML 配置段：`enabled`（默认 true）/ `default_model`（默认 grok-4.20-auto）

### Changed
- OpenAI 兼容性增强：`ChatCompletionRequest` 默认 `extra="ignore"`，未知字段不再 422；
  显式接受 `n` / `stop` / `seed` / `frequency_penalty` / `presence_penalty` / `user` /
  `tools` / `tool_choice` / `response_format` / `stream_options` / `logprobs` / `metadata`
  等字段（仅校验通过，部分不实现语义）
- 流式响应支持 `stream_options.include_usage`：在 `[DONE]` 前发出 usage chunk
- `chat.completion` 响应增加 `system_fingerprint`、`usage.prompt_tokens_details` /
  `completion_tokens_details` 字段；`max_tokens` 截断时 `finish_reason=length`
- 多模态消息：`image_url` / `input_audio` 块转占位文本而非报错，未知块跳过
- 占位端点 `/v1/embeddings` `/v1/completions` `/v1/moderations` `/v1/audio/*`
  返回 501 + `not_implemented`，比 404 对客户端更友好
- 错误响应 `type` 按 HTTP status 分级：401→`authentication_error`、
  404→`not_found_error`、429→`rate_limit_error`、5xx→`api_error`/`server_error`
- HttpOnly Cookie 鉴权：新增 `/v1/auth/login` `/v1/auth/logout` `/v1/auth/whoami`，
  浏览器前端切到 cookie 通道并启用 CSRF Double Submit 校验
- access log 脱敏 filter：自动把 `?api_key=` query / `Authorization` header /
  `x-api-key` header 里的值替换为 `***`

### Changed
- 模型注册表（`/v1/models`）增加 `permission` / `root` / `parent` 占位字段以提升 SDK 兼容度
- `images/generations` 显式拒绝 `response_format=b64_json`，返回 `unsupported_parameter`

### Security
- **移除 `/v1/grok/assets/download` 的 `?api_key=xxx` query 鉴权通道**：消除 access log /
  DevTools / 截图 / 浏览器历史泄露 api_key 的路径，前端浏览器改走 HttpOnly cookie，
  其它客户端继续用 `Authorization: Bearer` / `X-Api-Key` Header
- 前端不再把 api_key 写入 `localStorage` / DOM URL，统一由 cookie 持有

## [0.1.0] - 2026-04-28

Initial public release.

### Added
- OpenAI-compatible REST endpoints: `/v1/chat/completions`, `/v1/images/generations`, `/v1/videos/generate`, `/v1/models`
- Single-page Web UI: chat, continuous image gen, task queue, gallery, files, logs, settings
- `curl_cffi` chrome142 TLS fingerprint impersonation
- FlareSolverr-based `cf_clearance` auto-refresh (session_keeper, ~10 min interval)
- SQLite request logs (chat / image / video) with retention cleanup
- cURL / HAR import for one-click cookie configuration
- Per-session image gallery + waterfall layout, PhotoSwipe lightbox
- Click-to-fullscreen video modal across feed / gallery / files
- Grok cloud Files browser (waterfall + infinite scroll)
- Sync `grok_browser` with FlareSolverr UA major version automatically
- Pure-TOML configuration (no env-var lookups)
- Multi-arch Docker image (linux/amd64 + linux/arm64)

### Security
- Upstream 401 rewritten to 502 `upstream_unauthorized` to avoid false logout
- All cookies masked in admin / logs responses
- `data/config/mini.toml` and `.env` `.gitignore`-protected
