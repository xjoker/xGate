# xGate

> OpenAI-compatible API gateway for **xAI Grok web** — chat, image, video, queue, gallery, with built-in admin Web UI and FlareSolverr-based Cloudflare auto-refresh.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-multi--arch-blue?logo=docker)](#quick-start-docker)
[![GHCR](https://img.shields.io/badge/ghcr.io-xjoker%2Fxgate-blue?logo=github)](https://github.com/xjoker/xGate/pkgs/container/xgate)

---

## What it does

xGate 把 [grok.com](https://grok.com) 的网页接口包装成 **OpenAI 兼容 REST API**（`/v1/chat/completions`、`/v1/images/generations`、`/v1/videos/generate`），让任何已支持 OpenAI 协议的客户端无缝接入 Grok。

附带一套单页 Web 管理界面（Vanilla JS，无构建步骤）：聊天 / 连续生图 / 视频 / 任务队列 / 图库 / 日志 / 文件管理 / 设置。

### 关键特性

- **OpenAI Schema**：`/v1/models`、`/v1/chat/completions`（流式 + 非流式）、`/v1/images/generations`、`/v1/videos/generate`
- **TLS 指纹伪装**：基于 [`curl_cffi`](https://github.com/lexiforest/curl_cffi) 的 Chrome 142 指纹，不被 CF 拦截
- **Cloudflare 自动绕过**：通过外部 [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) 实例定期刷新 `cf_clearance`，无需手动维护
- **会话级图库**：每个 prompt 一个 session，按时间瀑布流展示，PhotoSwipe 灯箱看图、视频模态框看视频
- **持续生图 / 任务队列**：后台 worker 按目标张数排队，支持暂停/恢复/重排/优先级
- **请求日志**：聊天、图片、视频全量埋点，SQLite 存储，可按类型/关键字检索
- **完全配置文件驱动**：`data/config/mini.toml` 单一数据源，无环境变量魔法
- **凭据管理**：浏览器 cURL 一键导入登录态，写入即生效，cookie 脱敏展示
- **OpenAPI / Swagger**：`/docs` 自动文档

---

## Architecture

```mermaid
graph LR
    Client["Client SDK / Web UI\nChatGPT-Next / Cherry Studio"]
    xGate["xGate\nFastAPI + curl_cffi\nchrome142 TLS fingerprint"]
    Grok["grok.com\nCloudflare WAF"]
    FS["FlareSolverr\nreal Chromium 142"]

    Client -->|"OpenAI-compat REST\n/v1/chat  /v1/images  /v1/videos"| xGate
    xGate -->|"HTTPS (chrome142 JA3/JA4)"| Grok
    FS -->|"cf_clearance + __cf_bm\nevery ~10 min"| xGate
    FS <-->|"solve CF challenge"| Grok
```

### 为什么需要 FlareSolverr？

Cloudflare 的 `cf_clearance` cookie 与浏览器的 JA3/JA4 TLS 指纹绑定。`curl_cffi` 模拟的 chrome142 与 FlareSolverr 内置的真实 Chromium 142 指纹一致，所以 FlareSolverr 拿到的 `cf_clearance` 可以直接被 xGate 复用 — 这是当前能在 Linux Docker 内稳定通过 CF 的唯一可靠方案。

xGate 的 session_keeper 每 ~10 分钟通过 FlareSolverr 取一次新 `cf_clearance` + `__cf_bm`，与用户登录态（`sso`/`sso-rw`）合并，写回 `mini.toml`。

---

## External Dependencies

xGate 自身是无状态轻量服务，但运行时依赖以下外部组件：

| 组件 | 是否必须 | 说明 |
|------|----------|------|
| **grok.com 登录态** | 必须 | 需要已登录的 Grok 账号，通过 Web UI「导入 cURL」注入 |
| **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)** | 强烈建议 | 自动刷新 `cf_clearance`；缺少时只能靠心跳保活，稳定性大幅下降 |
| **SOCKS5 / HTTP 代理** | 视情况 | 若部署节点直连 grok.com 受限（常见于 VPS/大陆），必须配置；需与 FlareSolverr 使用同一出口 IP |

> **部署顺序建议**：代理 → FlareSolverr（指向同代理出口）→ xGate（配置相同代理 + FlareSolverr 地址）。

---

## Quick Start (Docker)

镜像托管在 **GitHub Container Registry**，无需 Docker Hub 账号即可拉取：

```bash
docker pull ghcr.io/xjoker/xgate:latest
```

### 1. 准备一个可达的 FlareSolverr

最简单的方式：在另一台机器（或同机不同端口）启动官方镜像：

```bash
docker run -d --name flaresolverr \
  -p 8191:8191 \
  -e LOG_LEVEL=info \
  --restart unless-stopped \
  ghcr.io/flaresolverr/flaresolverr:latest
```

> ⚠️ **强烈建议** xGate 与 FlareSolverr **使用同一出口 IP / 同一代理**，否则 `cf_clearance` 因 IP 不匹配会失效。最推荐：FlareSolverr 部署在 SOCKS5 代理所在的同一台机器上，xGate 通过 SOCKS5 出网。

### 2. 准备 xGate 配置

创建配置目录并下载示例配置（**不需要 clone 整个仓库**）：

```bash
mkdir -p xgate/data/config && cd xgate
curl -fsSL https://raw.githubusercontent.com/xjoker/xGate/main/data/config/mini.toml.example \
  -o data/config/mini.toml
curl -fsSL https://raw.githubusercontent.com/xjoker/xGate/main/docker-compose.yml \
  -o docker-compose.yml
# 编辑 mini.toml，至少填好 api_key、flaresolverr_url、proxy
```

最关键的几行：
```toml
[auth]
api_key = "请改成你自己的随机串"

[grok]
proxy = "socks5://user:pass@proxy.example.com:1080"   # 与 FlareSolverr 一致
flaresolverr_url = "http://flaresolverr.example.com:8191"   # FlareSolverr 实例
```

### 3. 启动

```bash
docker compose up -d
```

打开 `http://localhost:8024`，输入 `api_key` 登录。

### 4. 注入 Grok 登录态

1. 浏览器登录 https://grok.com
2. DevTools → Network → 找到任意 `grok.com` 请求 → 右键「Copy as cURL (bash)」
3. xGate UI → 设置 → 「导入 cURL」粘贴 → 服务端会立即冒烟验证
4. 验证通过即可使用所有功能；后台 session_keeper 会自动保活 cf_clearance

---

## Local Dev (without Docker)

```bash
# Python 3.13 + uv
uv sync
cp data/config/mini.toml.example data/config/mini.toml
# 编辑 mini.toml
uv run xgate
```

---

## Configuration Reference (`data/config/mini.toml`)

| Key | Default | 说明 |
|-----|---------|------|
| `server.host` | `0.0.0.0` | 监听地址 |
| `server.port` | `8024` | 监听端口 |
| `auth.api_key` | `change-me` | Web UI 登录密码 + Bearer token |
| `grok.cookie` | _空_ | 完整 grok.com cookie（推荐 UI 导入） |
| `grok.user_agent` | Chrome 142 UA | 必须与 FlareSolverr Chromium 大版本一致 |
| `grok.browser` | `chrome142` | curl_cffi 指纹（chrome142 / chrome145 / chrome146） |
| `grok.proxy` | _空_ | SOCKS5 / HTTP 代理 |
| `grok.timeout_seconds` | `120` | 单次 Grok 请求超时 |
| `grok.flaresolverr_url` | _空_ | FlareSolverr 地址；空则只能心跳保活 |
| `grok.flaresolverr_proxy_url` | _空_ | 让 FlareSolverr 走的 HTTP 代理（同机部署可留空） |
| `log.retention_days` | `90` | SQLite 日志保留天数 |

> ⚠️ 该文件 **不得入仓**（已 `.gitignore`），含登录 cookie 和代理凭据。

---

## API Surface

完整 OpenAPI：`http://localhost:8024/docs`

### OpenAI 兼容
| Method | Path | 说明 |
|--------|------|------|
| GET    | `/v1/models` | 模型列表（grok-4.20-fast/auto/expert/heavy、grok-imagine、grok-imagine-pro） |
| POST   | `/v1/chat/completions` | 文本对话，支持 `stream=true` SSE |
| POST   | `/v1/images/generations` | 一次性生图（n 张） |
| POST   | `/v1/videos/generate` | 提交视频生成（同步等待生成完，1–3 分钟） |
| GET    | `/v1/videos/{id}/status` | 视频是否就绪 |

### 图片流 / 队列 / 图库
| Path | 说明 |
|------|------|
| `POST /v1/images/stream/start` | 启动后台连续生图 worker |
| `POST /v1/images/stream/stop` | 停止 |
| `GET  /v1/images/stream/status` | worker 状态 |
| `GET/POST /v1/images/tasks` | 任务队列 CRUD |
| `POST /v1/images/tasks/{id}/{pause,resume,move}` | 控制单任务 |
| `GET  /v1/images/sessions` | session 卡片列表（含 prompt、张数、模型 chip） |
| `DELETE /v1/images/sessions/{id}` | 隐藏 / 删除 session |
| `GET  /v1/images/gallery` | 全局或按 session 分页图库 |

### 文件 / 日志 / 管理
| Path | 说明 |
|------|------|
| `GET  /v1/files/image/{sid}/{fn}` | 本地图片直链（无需鉴权） |
| `GET  /v1/files/video/{sid}/{fn}` | 本地视频直链 |
| `GET  /v1/grok/assets` | Grok 云端文件分页（瀑布流） |
| `POST /v1/grok/assets/{id}/delete` | 删除 Grok File |
| `POST /v1/grok/assets/save-local` | 批量下载到本地 |
| `GET  /v1/quota` | 查询 Grok 账户额度 |
| `GET  /v1/logs` / `/v1/logs/stats` | 请求日志查询 |
| `POST /admin/import-curl` | 一键 cURL 导入 cookie + 冒烟 |
| `POST /admin/config` | 热更新部分配置项 |
| `GET  /admin/status` | 运行状态 |

所有 `/v1/*` 与 `/admin/*` 都需要 `Authorization: Bearer <api_key>` 或 `x-api-key`。

---

## Web UI 截图

| 视图 | 说明 |
|------|------|
| 首页 Dashboard | 统计卡片 + 14 天柱图 + 余额 + 快捷链接 |
| 生成 | Speed/Quality/Video 三模式 + 比例 + 间隔 + 上限 + 实时 feed |
| 任务队列 | 优先级队列、暂停/恢复/重排、目标张数进度 |
| 图库 | 全局时间瀑布流、按 session 卡片侧栏、PhotoSwipe 看图、视频模态框 |
| 文件 | Grok 云端文件瀑布流，无限滚动，批量下载/删除 |
| 日志 | 聊天/图片/视频分类筛选，prompt 全文搜索 |
| 设置 | cURL 导入、API Key/代理修改、cookie 脱敏 |

---

## Security Notes

- **永远不要把 `data/config/mini.toml` 推上公开仓库**（已 `.gitignore`）
- `api_key` 默认 `change-me`，必须改成强随机串
- xGate 自身鉴权失败返回 401；上游 Grok 返回的 401 会被改写为 502 `upstream_unauthorized`，避免误踢登录
- Cookie 在所有响应里都脱敏（`mask_secret(value, keep=6)`）
- 日志 SQLite 自动按 `log.retention_days` 滚动清理

---

## FAQ

**Q: 为什么不用 Playwright / Selenium 直接控制浏览器发请求？**
A: 浏览器单实例并发能力差，长期运行内存巨大；FlareSolverr + curl_cffi 在保留 Chrome TLS 指纹优势的同时，让 xGate 本身保持 async + 高并发。

**Q: 必须要走代理吗？**
A: 不强制。如果你和 Grok 网络通畅、CF 不会因为机房 IP 把你 challenge 死，可以不配。一旦配了，**xGate 与 FlareSolverr 必须同 IP 出口**。

**Q: cf_clearance 多久会失效？**
A: CF 颁发的 cf_clearance 通常 30 分钟–24 小时；session_keeper 默认 10 分钟 ± 2 分钟刷一次，远低于失效周期。

**Q: 视频生成接口为什么阻塞 1–3 分钟？**
A: 当前实现等 SSE 100% 后才返回 `video_id`。Web UI 已做了"提交即显示 pending 卡片 + 禁用按钮 + 旋转图标"的反馈，调用方代码也建议把 timeout 设长一些。

**Q: 支持多账号吗？**
A: 当前一份 cookie 一个账号；多账号轮询不在 v0.x 范围。

---

## Development

```bash
uv sync
uv run pytest tests/
uv run xgate
```

代码组织：
```
src/mini_grok_api/
├── main.py            # FastAPI 路由 + session_keeper
├── grok_client.py     # 所有 Grok API 调用 + FlareSolverr 集成
├── config.py          # mini.toml 加载（不读环境变量）
├── ws_gateway.py      # 单例 WebSocket 网关，串行图片生成
├── image_stream.py    # 后台连续生图 worker
├── task_queue.py      # 优先级任务队列
├── db.py              # SQLite chat/image/video logs
├── curl_import.py     # cURL 解析
├── har_import.py      # HAR 解析
├── monitor.py         # 内存运行状态
├── openai_compat.py   # OpenAI 响应格式工具
├── schemas.py         # Pydantic 请求 / 响应模型
├── models.py          # 模型注册表
└── static/index.html  # 单页 Web UI
```

---

## Acknowledgements

- [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) — Cloudflare bypass core
- [`curl_cffi`](https://github.com/lexiforest/curl_cffi) — TLS-fingerprint-faithful HTTP client
- [PhotoSwipe](https://photoswipe.com/) — Web UI 图片灯箱
- [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/)

---

## License

MIT © 2026 xjoker
