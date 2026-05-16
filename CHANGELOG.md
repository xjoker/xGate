# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] - 2026-05-16

### Security
- **CI 引入依赖 CVE 扫描**：`test.yml` 加 `pip-audit --strict` 步骤，与 pytest 并列，
  发现任何 CVE 或元数据缺失立即阻断 merge。和 dependabot 互补（dependabot 推 PR，
  pip-audit 卡守门）。
- 依赖审计 + 升级（2026-05-16）：`pip-audit --strict` 当前 0 个 CVE。
  `uv lock --upgrade` 升级 13 个 transitive 包到最新 patch/minor：
  cryptography 47→48、idna 3.13→3.15、markdown-it-py 4.0→4.2、mcp 1.27.0→1.27.1、
  orjson 3.11.8→3.11.9、propcache 0.4.1→0.5.2、pydantic 2.13.3→2.13.4、
  pydantic-core 2.46.3→2.46.4、pydantic-settings 2.14.0→2.14.1、
  requests 2.34.1→2.34.2、sse-starlette 3.4.1→3.4.4、tiktoken 0.12.0→0.13.0、
  uvicorn 0.46.0→0.47.0。

### Added
- 账号管理「编辑」入口：复用账号 modal，支持只改 priority/weight 而保留旧 cookie
  （后端 `_AccountUpsertRequest.cookie` 改为可选；为空 + label 已存在则保留旧值）
- 账号管理表格新增「额度概览」列，按账号 × 模型展示后台 poll 缓存的剩余配额
- `/v1/quota/chat` `/v1/quota` `/admin/dashboard` 响应新增 `per_account` 字段，
  完全复用账号池现有 quota_cache，**不增加上游 Grok 请求量**
- 首页配额监控加「按账号展开」toggle（≥ 2 个账号时显示）
- **`/v1/auth/login` 限流（slowapi 内存后端，10 次/分钟/IP）**：超限返回 429 +
  `Retry-After` header + `error.type=rate_limit_error, code=login_rate_limited`；
  正常响应也带 `X-RateLimit-*` 头方便客户端预判。对齐 Phase 3 backlog #1。
- **per-account image quota**：后台 quota poll loop 每账号额外查一次图片配额，
  缓存到 `accounts.quota_cache_json['__image__']`；全局 `_IMAGE_QUOTA_MODEL_HINT`
  避免对每个账号重跑 4-candidate 探测（首次跑成功后所有账号复用）。
  - `account_pool.update_image_quota / get_image_quota` 新方法
  - `/v1/quota/image` 响应新增 `per_account` 字段
  - `/admin/dashboard` 响应新增 `per_account_image_quota` 字段
  - 首页「按账号展开」视图为每个账号加图片配额 chip（粉色，区别于 chat chip）
- **GitHub Actions 升级到最新 major（Phase 3 #5，Node 20 deprecation 截止 2026-09-16）**：
  - `actions/checkout` v4 → v6（Node 24）
  - `actions/setup-python` v5 → v6
  - `astral-sh/setup-uv` v5 → v8.1.0（immutable tag — v8+ 不发布 `@v8` 浮动 tag，
    supply-chain 防护；下次升级时需手动 bump 此处的 patch 版本）
  - `docker/setup-qemu-action` v3 → v4
  - `docker/setup-buildx-action` v3 → v4
  - `docker/login-action` v3 → v4
  - `docker/metadata-action` v5 → v6
  - `docker/build-push-action` v6 → v7
- **`X-Account-Label` 客户端 header**：API 客户端可指定 header 强制走某账号
  （debug 试号 / 多轮对话 sticky 防 LRU 切号失忆）。覆盖 8 个用户面 endpoint：
  chat completions、video generate/status、quota×3、chat-imagine。
  - 新异常 `UnknownAccountError` / `AccountDisabledError`，handler 翻译为 400 +
    `error.code=account_label_not_found / account_label_disabled`（strict 语义）
  - `account_pool.acquire(force_label=...)` 在 label 不存在 / 被禁用时 raise
    （之前会静默回退到 settings_fallback）
  - image generations 仍走 LRU（透传需要 ws_gateway 协议扩展，留后续 PR）

### Fixed
- 设置页「保存配置」silent failure：`apiForm` 不抛非 2xx，导致 CSRF 失败时仍弹「已保存」
  → 新增 `apiFormJson` 与 `api()` 行为对齐，`saveConfig` / `importCurl` 都走它
- 账号管理 modal 背景点击关闭事件由 `document` 级改为绑在 modal 自身，
  避免事件冒泡导致的误关
- 「修改账号有问题」：之前 admin UI 只能 +添加/删除/启停，priority/weight 必须
  删了重建。本次补齐 edit modal，对齐 Phase 3 backlog #2
- **BUG-A** `/admin/import-curl`（旧 single-user 端点）写 `settings.grok_cookie`
  后未调用 `account_pool.import_from_settings(force_refresh_default=True)`，导致
  `default` 账号 cookie 不刷新 — UI 提示"导入成功"但实际请求仍走旧 cookie。
  现与 `/admin/config` 行为对齐。+1 集成测试。
- **BUG-B** 账号管理「编辑/禁用/删除」按钮静默失效：inline `onclick` 用
  `JSON.stringify(esc(label))` 嵌入 HTML attribute，浏览器在第二个 ASCII `"`
  截断属性值；按钮看似可点但 onclick 调用变 no-arg。新增 `_attrJson` helper
  做 JSON.stringify + HTML 引号转义。修了 5 处同 pattern（含 PR-5 `_renderGrokImages`
  和 `_parseMd` 中的 img onclick）。
- **BUG-C** 新加 / 编辑账号后 `quota_cache` 要等 5 分钟（poll loop 周期）才填充，
  UI per_account 视图空窗期太长。`/admin/accounts` POST 和
  `/admin/accounts/import-curl` 现在 fire-and-forget schedule 一次 async warmup
  (`_poll_one_account_quota`)，新账号秒级出现在 per_account 视图。+2 集成测试。

### Changed
- 设置页 UI 拆分单/多用户：
  - 「导入 cURL」→「快速导入 default 账号」，明确告知此操作覆盖 default 账号
  - 「手动配置」→「全局配置」，Cookie/UA/指纹移到下方 dashed border 区域
    并明示这些字段是「快捷编辑 default 账号」，多账号应改用「账号管理」


## [0.2.0] - 2026-05-14

### Added
- **多账号池（Phase 1 + 2）**：
  - `accounts.AccountPool` 凭证 + 运行时状态持久化到 SQLite
  - 选号策略：priority asc + LRU；soft_cooldown（quota 剩余 < 5% 自动避让）
  - 故障决策表：rate_limit / cf_challenge / unauthorized / 5xx 各自 cooldown 时长
  - 上游 `retry-after` / `x-ratelimit-reset-*` 解析，分级 cooldown（minute / hour / day）
  - 后台 quota poll loop（5min 一轮）+ auto_disabled re-validate loop（30min 探活）
  - 0-账号兜底：mini.toml 的 `grok.cookie` 自动 import 为 default 账号
  - admin/config 改凭证字段时自动同步 default 账号（保留 enabled/priority/weight）
  - Monitor 按 account_label 分桶；4 张日志表加 `account_label` 列（ALTER 幂等迁移）
- **OpenAI 兼容增强**：
  - `tools` / `tool_choice` 单轮 function calling（prompt 注入 + 响应解析）
  - 多轮 `role=tool` 消息正确翻译（含 tool_call_id → name 反查）
  - `tiktoken` 精确 token 估算（替代 `len // 4`），CL100K base encoder
- **认证体验**：
  - sliding session（剩余 TTL < 50% 自动续期）
  - 401 reason codes（`session_missing` / `session_revoked` / `api_key_invalid`）
  - 前端登录失效提示按 code 分类显示
  - admin/accounts CRUD endpoints + 设置页账号管理面板（增/删/启用禁用/cURL 导入）
- **REST API**：
  - `GET /admin/accounts` 列出账号 + `POST /admin/accounts` 增删
  - `POST /admin/accounts/import-curl` 从 cURL 创建新账号
  - `GET /v1/logs?account_label=...` 按账号过滤日志
  - `/admin/dashboard` 增加 `accounts` + `monitor.per_account` 字段
- **配置**：
  - `[server] cookie_secure`（auto/always/never）
  - `[grok] statsig_id`（HAR/cURL 导入自动抓取）
  - `[grok] disable_ssl_verify`（默认 False，应急用）
- GitHub Actions CI（pytest 自动跑）

### Changed
- `grok_client._ws_connect` / `ws_gateway` / `get_video_link` 默认启用 SSL 校验
  （Round 1 SAST：之前无条件 `ssl=False` 让上行 Cookie 暴露于 MitM）
- `/v1/quota/chat` `/v1/quota/image` `/v1/quota` `/admin/models/verify`
  `/admin/dashboard` 5 个 GET endpoint 改为 POST（CSRF 防护）
- 文件接口（`/v1/files/image` `/v1/files/video` `/v1/grok-files/{fn}`）改用
  HMAC signed URL（`?sig=...&exp=...`），1h TTL；老 URL 仍兼容（warning + 放行）
- `/v1/files/proxy` 保留 GET（浏览器 img 需要）但 cookie 通道强制 same-origin Referer
- secure cookie 标志改为启动一次性判定（按 `cookie_secure` 配置 + `public_base_url`）
- SameSite=Strict → Lax（避免外部链接跳入丢 cookie）
- SQLite 路径绝对化（基于项目根，systemd / Docker WorkingDir 变化不丢数据）
- `/docs` `/redoc` `/openapi.json` 自定义路由 + `_require_api_key` 鉴权
- 测试用例修复 + 大幅扩充：125 → 256 passed
- DELETE 500 响应不再回显原始异常

### Fixed
- **空 / 默认 `api_key` 启动 hard fail**（非 127.0.0.1 绑定 → SystemExit）
- **登录偶尔被踢出**（reverse-proxy / HSTS 切换导致 secure 标志抖动）
- 登录 24h 硬过期（无 sliding 续期）
- MCP `_BearerAuthMiddleware` `==` 比较 → `secrets.compare_digest`（消除时序攻击）
- 前端 `_parseMd` Markdown 图片 + `openVideoModal` URL 协议白名单（防 `javascript:` XSS）
- `esc()` helper 加单引号转义（一处修复多处受益）
- `python-multipart` 升 0.0.27+（CVE: DoS via unbounded multipart headers）
- `tests/test_auth.py` 全红（引用旧内存版 session）→ 迁移到 SQLite `revoke_all`

### Security
- 启动校验：弱 api_key + 公网绑定 → 拒绝启动
- mini.toml 权限警告（POSIX 0o077 任意位可读 → logger.warning）
- 401 reason code 不泄露内部状态（合并 expired 和 revoked 为同一信号）

## [0.1.x] - 2026-04 / 05

详见各 patch commit（0.1.1 - 0.1.5）：MCP Streamable HTTP（9 tools）、HttpOnly Cookie 鉴权、
WebUI 重构（创作 / 资产 / 对话 / 媒体）、动态模型注册表、Dashboard、配额查询、access log 脱敏、
按模型配额面板、日志分页、详细日志、模型 ID 修复等。

<details>
<summary>0.1.x 累计变更摘要</summary>

- **MCP Streamable HTTP 接入**（`/mcp`）：9 个 tool（grok_chat / x_search / web_search /
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
- 移除 `/v1/grok/assets/download` query 鉴权通道，浏览器走 HttpOnly cookie
- 模型注册表加 OpenAI 占位字段、`images/generations` 拒绝 `b64_json`

</details>

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
