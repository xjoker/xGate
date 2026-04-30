# xGate

> Grok Web 图片 / 视频生成与文件管理工具；提供 OpenAI 兼容 API 与 MCP 服务，可直接接入 Claude Code、Codex 等 AI 编程助手。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![GHCR](https://img.shields.io/badge/ghcr.io-xjoker%2Fxgate-blue?logo=github)](https://github.com/xjoker/xGate/pkgs/container/xgate)

![Dashboard](docs/screenshots/dashboard.png)

## 适用场景

Grok 网页适合偶尔生几张图。当你需要批量跑同一 prompt 挑稿、做系列素材、归档云端 Files，或者希望在 Cherry Studio、ChatGPT-Next 这类客户端里直接调用 Grok 时，仅靠网页就比较吃力——结果散在聊天记录里难以回溯，被内容审核拦截后需要手动重提，云端文件也只能逐个清理。

xGate 把这些操作收敛到一个 Web UI 里：

| 场景 | 直接用 grok.com | 使用 xGate |
| --- | --- | --- |
| 批量生图 | 手动重复提交 | 目标张数队列，可暂停 / 恢复 / 重试 |
| Prompt 实验 | 结果混在聊天记录里 | 每个 prompt 自动归到独立 session |
| 结果管理 | 留在 Grok 云端 | 自动落地到本地 `data/images/` |
| 云端 Files | 逐个处理 | 同步、批量保存、批量删除 |
| 外部调用 | 无 OpenAI 入口 | `/v1/chat/completions`、`/v1/images/generations` |
| Cookie / CF | 手动维护登录态 | cURL 导入，可选 FlareSolverr 自动刷新 |

## 主要功能

- **生成**：图片 / 视频共用一套界面，支持参考图、自定义比例，以及 `Speed`、`Quality`、`Chat` 三种模式切换
- **任务队列**：状态持久化于 SQLite，支持暂停、恢复、重试、排序与批量操作；针对易被内容审核拦截的 prompt 提供"尽力模式"
- **图库**：每次生成自动按 prompt 创建 session，图片与视频归入同一本地图库
- **Grok Files 管理**：同步云端列表、批量下载到本地、清理云端文件
- **反 Cloudflare**：基于 `curl_cffi` 模拟 Chrome TLS 指纹；登录态通过 Chrome cURL 一键导入；可选接入 FlareSolverr 定时刷新 `cf_clearance`
- **OpenAI 兼容接口**：`/v1` 路径下提供标准 endpoint，外部客户端可直接接入
- **MCP 工具服务**：实现 MCP 2025-06-18 Streamable HTTP 协议，暴露 9 个工具（对话、搜索、生图、配额查询等），可在 Claude Code / Codex 中直接调用 Grok 能力
- **极简部署**：单文件配置 `data/config/mini.toml`，单目录持久化 `/app/data`

## 界面预览

<table>
  <tr>
    <td align="center"><img src="docs/screenshots/gen.png" alt="Generation"/><br/><b>生成</b><br/>图片 / 视频生成、参考图、比例、目标张数、加入队列</td>
    <td align="center"><img src="docs/screenshots/images.png" alt="Gallery"/><br/><b>图库</b><br/>按 prompt 会话归档，瀑布流预览、下载、删除</td>
  </tr>
  <tr>
    <td align="center"><img src="docs/screenshots/files.png" alt="Files"/><br/><b>Files</b><br/>Grok 云端文件同步、预览、保存本地、云端删除</td>
    <td align="center"><img src="docs/screenshots/logs.png" alt="Logs"/><br/><b>日志</b><br/>聊天、图片、视频、系统事件检索</td>
  </tr>
  <tr>
    <td align="center"><img src="docs/screenshots/chat.png" alt="Chat"/><br/><b>聊天</b><br/>直接与 Grok 对话，也可触发生图</td>
    <td align="center"><img src="docs/screenshots/settings.png" alt="Settings"/><br/><b>设置</b><br/>导入 cURL、配置代理、FlareSolverr、API Key</td>
  </tr>
</table>

## 典型用法

1. 在浏览器登录 Grok，复制一份 cURL 导入 xGate
2. 在 Web UI 中提交生图任务、队列或视频生成
3. 生成结果自动落地到本地 `data/images/`
4. 在图库中按 session 浏览、筛选、下载或删除
5. 同步 Grok 云端 Files，将需要保留的素材下载到本地，再批量清理云端

## 功能详情

### 图片 / 视频生成

图片提供 `Speed`（`grok-imagine`）与 `Quality`（`grok-imagine-pro`）两档画质，并可通过 `Chat` 模式经由聊天上下文触发生图。支持上传参考图、自定义比例、设置目标张数后自动连续生成。每轮生成会按 prompt 自动归入独立 session，图片与视频共享同一图库视图。

### 任务队列

将 prompt 加入队列后即可按目标张数批量生成，支持暂停、恢复、重试与顺序调整，也可批量操作。任务状态区分成功、失败与内容审核拦截。对于易被审核拦截的 prompt，可启用"尽力模式"，持续尝试直至预算耗尽。

### 图库

按 session 展示每次生成的 prompt、模型、比例与产物，瀑布流配合灯箱预览，支持全选、删除以及整 session 下载。底层文件直接位于 `data/images/`，重建容器不会丢失。

### Grok 云端 Files 管理

同步 Grok 云端文件列表（包含 Imagine 产物），支持全量 / 立即 / 自动同步，可批量下载至 `data/grok-files/`，并提供本地图片与视频预览、幻灯片播放、浏览器下载。删除操作仅清理云端记录，已下载到本地的文件不会受影响。

### OpenAI 兼容 API

核心 endpoint：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/images/generations`
- `POST /v1/videos/generate`

辅助 endpoint（管理后台同样使用）：

- `GET /v1/quota`、`GET /v1/quota/chat` — 查询 Grok 账号配额
- `GET /v1/logs`、`GET /v1/logs/stats` — 检索请求日志与统计

完整 OpenAPI 文档见 `/docs`。

## 工作原理

后端基于 FastAPI，前端为 Vanilla JS 单页，无构建步骤。配置仅从 `data/config/mini.toml` 读取，Web UI 中的修改也会写回同一文件。

图片生成统一经由 `WsGateway`：同一时刻仅保持一个 Grok 图片 WebSocket，断线后自动重连并继续当前任务。队列、日志、Grok Files 元数据存放于 SQLite (`data/file/xgate.db`)。生成产物按 session 存入 `data/images/<session_id>/`，目录内的 `session.json` 记录 prompt、来源与比例。Grok 云端文件既可作为代理仅供预览，也可加入后台下载队列保存到 `data/grok-files/`。

未配置 FlareSolverr 时，xGate 通过 `rest/skills` 心跳保活；配置后约每 10 分钟自动刷新一次 Cloudflare cookie。

## 快速部署

建议只维护一个宿主机数据目录，例如 `/data/Work/xgate`，容器内固定挂载到 `/app/data`。配置文件的实际路径为：

```text
/data/Work/xgate/config/mini.toml
```

### 1. 准备 mini.toml

```bash
mkdir -p /data/Work/xgate/config
curl -fsSL https://raw.githubusercontent.com/xjoker/xGate/main/data/config/mini.toml.example \
  -o /data/Work/xgate/config/mini.toml
```

至少修改 `auth.api_key`：

```toml
[auth]
api_key = "换成你的长随机密码"

[grok]
# 首次启动可留空，登录 Web UI 后通过「设置 → 导入 cURL」写入
cookie = ""

# 访问 grok.com 需要代理时填写，不需要则留空
proxy = ""

# 仅在部署了 FlareSolverr 时填写
flaresolverr_url = ""

[log]
# 请求日志保留天数
retention_days = 90
```

其余字段保留默认值即可。通过 Web UI 导入 cURL 后，服务会自动把 cookie、User-Agent 与指纹信息写回 `mini.toml`；Grok Files 自动同步开关位于 `[files]` 段，可在 Web UI 中切换。

### 2. docker compose

新建 `docker-compose.yml`：

```yaml
services:
  xgate:
    image: ghcr.io/xjoker/xgate:latest
    platform: linux/amd64
    ports:
      - "127.0.0.1:8024:8024"
    volumes:
      - /data/Work/xgate:/app/data
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped
```

将 `127.0.0.1` 替换为实际监听 IP；如需在所有网卡监听，写作 `"8024:8024"` 即可。

启动服务：

```bash
docker compose up -d
```

访问地址：

```text
http://127.0.0.1:8024
```

登录密码即 `mini.toml` 中的 `auth.api_key`。

> 当前版本不再使用 `ADMIN_PASSWORD`；Web UI 与 API 的鉴权统一读取 `data/config/mini.toml` 的 `auth.api_key`。

## 导入 Grok 登录态

### 三端必须同出口 IP

导出 cURL 的浏览器、xGate 访问 Grok 的出口、FlareSolverr 过 Cloudflare 的出口，必须是**同一个公网 IP**。这是最常见的 403 来源——`cf_clearance` 同时绑定来源 IP 与浏览器指纹，从 A 机器导出的 cookie 在 B 机器上基本无法使用。

| 组件 | 要求 |
| --- | --- |
| 导出 cURL 的浏览器 | 与 xGate 走相同的代理出口 |
| xGate | `grok.proxy` 为空时使用容器所在机器的出口；不为空时使用代理出口 |
| FlareSolverr | 与 xGate 保持同一出口 IP |

举一个反例：在家里电脑直连 Grok 导出 cURL，再把 cookie 放到海外 VPS 上使用——除非家里浏览器、VPS 上的 xGate、FlareSolverr 都通过同一个代理出口访问 Grok，否则无法工作。

操作步骤：

1. 在 Chrome 中打开并登录 <https://grok.com>
2. 打开 DevTools 的 Network 面板
3. 选择任意 `grok.com` 请求（推荐 `rest/skills`）
4. 右键请求，选择 Copy → Copy as cURL (bash)
5. 打开 xGate Web UI，进入「设置 → 导入 cURL」
6. 粘贴 cURL 并导入，冒烟验证通过后即可使用

> 建议在导入前于浏览器中访问任意 IP 查询网站，确认公网 IP 与 xGate / FlareSolverr 出口一致。

## OpenAI 客户端接入

**Base URL**

```text
http://127.0.0.1:8024/v1
```

**API Key**

使用 `mini.toml` 中的 `auth.api_key`。

**连通性验证**

```bash
curl http://127.0.0.1:8024/v1/models \
  -H "Authorization: Bearer 你的-api-key"
```

**常用模型**

- `grok-4.20-fast`
- `grok-4.20-auto`
- `grok-4.20-expert`
- `grok-4.20-heavy`
- `grok-4.3-beta`
- `grok-imagine`
- `grok-imagine-pro`

完整接口文档见 `http://127.0.0.1:8024/docs`。

## MCP 接入

xGate 实现 [MCP 2025-06-18 Streamable HTTP](https://spec.modelcontextprotocol.io/) 协议，将 Grok 能力暴露为 9 个 MCP tool，可直接在 Claude Code、Codex 等支持该规范的 AI 助手中使用。

**端点**

```text
http://127.0.0.1:8024/mcp
```

**鉴权**

所有请求须带 `Authorization: Bearer <api_key>`，其中 `api_key` 与 OpenAI 接口共用同一个 `auth.api_key`。

**工具列表**

| 工具 | 功能 |
|------|------|
| `grok_chat` | 多轮对话，返回完整答复 + 搜索结果 / 引用 / 推理步骤；同 MCP session 内自动续轮 |
| `grok_x_search` | X 高级搜索，15+ 过滤参数，返回结构化推文列表（不含 LLM 总结） |
| `grok_web_search` | Web 搜索，返回 url / title / preview 结构化结果 |
| `grok_quota` | 查询指定模型剩余配额 |
| `grok_imagine` | 图片生成（chat 通道），支持 url / local_path / base64 三种返回模式 |
| `grok_imagine_video` | 视频生成（1-5 分钟），本地缓存后返回代理 URL 或本地路径 |
| `grok_files_list` | 列出 Grok 云端文件（实时查询） |
| `grok_files_save_local` | 将指定云端文件下载到本地 `data/grok-files/` |
| `grok_files_delete` | 从 Grok 云端删除文件 |

详细参数 schema、返回示例与 prompt 模板见 [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md)。

### Claude Code

**方式一：CLI 一键添加（推荐）**

```bash
claude mcp add --transport http xgate http://127.0.0.1:8024/mcp \
  --header "Authorization: Bearer 你的-api-key"
```

**方式二：编辑配置文件**

编辑 `~/.claude/mcp.json`：

```json
{
  "mcpServers": {
    "xgate": {
      "type": "http",
      "url": "http://127.0.0.1:8024/mcp",
      "headers": {
        "Authorization": "Bearer 你的-api-key"
      }
    }
  }
}
```

验证：`claude mcp list`

### Codex

**方式一：编辑配置文件**

编辑 `~/.codex/config.toml`，在 `[mcp_servers]` 下追加：

```toml
[mcp_servers.xgate]
enabled = true
transport = { type = "streamable_http", url = "http://127.0.0.1:8024/mcp" }
bearer_token_env_var = "XGATE_API_KEY"
```

然后导出 key：`export XGATE_API_KEY=你的-api-key`

**方式二：CLI 命令（通过 mcp-remote 桥接）**

`codex mcp add` 只支持 stdio transport。xGate 提供 `/mcp/sse` SSE 端点，配合 [`mcp-remote`](https://www.npmjs.com/package/mcp-remote)（stdio → HTTP 桥接）可用 CLI 一键添加：

```bash
# 需要 Node.js（用于 npx）；mcp-remote 作为桥接层，无需单独安装
export XGATE_API_KEY=你的-api-key
codex mcp add xgate \
  --env XGATE_API_KEY="$XGATE_API_KEY" \
  -- npx -y mcp-remote http://127.0.0.1:8024/mcp/sse \
     --header "Authorization:${XGATE_API_KEY}" \
     --allow-http
```

验证：`codex mcp list`

### 连通性验证

```bash
curl -s -X POST http://127.0.0.1:8024/mcp \
  -H "Authorization: Bearer 你的-api-key" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  | python3 -m json.tool
```

### 注意事项

- MCP 可通过 `mini.toml` 的 `[mcp].enabled = false` 禁用；禁用后 `/mcp` 路径返回 404，其余路由不受影响
- 所有 tool 返回 buffer 模式（完整结果一次性返回）；流式需求请走 `/v1/chat/completions?stream=true`
- `grok_imagine` `return_mode=url` 返回的代理 URL（`/v1/files/proxy`）无需 cookie，MCP 客户端可直接访问生成图片
- 单 cookie 单账号，多客户端并发会快速耗尽配额；建议调用前先用 `grok_quota` 确认剩余额度

## 数据目录

只要持久化 `/app/data`，镜像升级后配置与生成结果都不会丢失。

| 宿主机路径 | 容器内路径 | 内容 |
| --- | --- | --- |
| `/data/Work/xgate/config/mini.toml` | `/app/data/config/mini.toml` | 配置、cookie、代理、API Key |
| `/data/Work/xgate/images/` | `/app/data/images/` | 生成的图片与视频 |
| `/data/Work/xgate/file/xgate.db` | `/app/data/file/xgate.db` | 请求日志、队列、Grok Files 元数据 |
| `/data/Work/xgate/grok-files/` | `/app/data/grok-files/` | 从 Grok 云端保存到本地的文件 |

> `mini.toml` 包含 cookie 与代理凭据，请勿提交到公开仓库。

## FlareSolverr 与代理

FlareSolverr 并非必需。未配置时，xGate 通过现有 cookie 做心跳保活；Cloudflare 重新挑战或 cookie 失效后，需要再次导入 cURL。如果希望长期无人值守，可部署 FlareSolverr 并在 `mini.toml` 中配置：

```toml
[grok]
proxy = "socks5://user:pass@proxy.example.com:1080"
flaresolverr_url = "http://flaresolverr:8191"
```

再次提醒：导出 cookie 的浏览器、xGate、FlareSolverr 三端必须保持同一出口 IP，任何一端出口不一致都可能立刻使 `cf_clearance` 失效。

## 本地开发

```bash
uv sync
cp data/config/mini.toml.example data/config/mini.toml
uv run xgate
```

运行测试：

```bash
uv run pytest tests/
```

本地服务默认监听 `0.0.0.0:8024`。如需更换端口，修改 `data/config/mini.toml` 中的 `server.port`。

## 安全建议

- 首次部署务必修改 `auth.api_key`
- `data/config/mini.toml` 含 cookie 与代理凭据，切勿提交到公开仓库
- `/v1/files/*` 静态资源路由不参与鉴权（设计上用于浏览器直链预览图片 / 视频），暴露到公网时需在反向代理层另行限制
- 不建议将服务直接暴露到公网；如有需要，请前置可信反向代理、访问控制与 HTTPS
- Grok 账号失效后，从 Chrome 重新导出 cURL 并导入即可恢复

## License

MIT © 2026 xjoker
