# MCP Tools 参考手册

xGate 通过 MCP Streamable HTTP（端点 `/mcp`）暴露以下 9 个 tool。所有 tool 均为 buffer 模式（完整结果一次性返回）。

---

## grok_chat

与 Grok 对话，返回完整答复 + 搜索结果 / 引用 / 推理步骤。

同一 MCP session 内不传 `conversation_id` 时自动续轮；显式传 `conversation_id=""` 强制开新会话。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `prompt` | `string` | 必填 | 用户消息 |
| `model` | `string` | `"grok-4.20-auto"` | 模型名，见 `/v1/models` |
| `conversation_id` | `string \| null` | `null` | 续轮时传上一轮的 `conversation_id`；`null`=自动续；`""`=强制开新对话 |
| `parent_response_id` | `string \| null` | `null` | 配合 `conversation_id` 显式指定父 response |
| `format` | `"rich" \| "openai"` | `"rich"` | 返回结构：`rich`=原生结构，`openai`=OpenAI ChatCompletion 格式 |
| `include_reasoning` | `boolean` | `false` | 是否在结果中包含推理 token（流量大，按需开启） |
| `include_search_results` | `boolean` | `true` | 是否在结果中包含 X / Web 搜索原始结果 |
| `disable_search` | `boolean` | `false` | 禁用 Grok 联网搜索 |
| `temporary` | `boolean` | `true` | 是否为临时（私有）对话，不保存到 Grok 历史 |

### 返回（format=rich）

```json
{
  "conversation_id": "conv-xxx",
  "response_id": "resp-yyy",
  "title": null,
  "text": "Grok 的回答正文...",
  "reasoning_steps": [
    {"step": 1, "rollout": "rollout-0", "label": "分析问题"}
  ],
  "reasoning_tokens": [],
  "tool_calls": [
    {"tool": "xSearch", "args": {"query": "..."}}
  ],
  "x_results": [...],
  "web_results": [...],
  "citations": [
    {"card_id": "card-1", "url": "https://example.com"}
  ],
  "follow_ups": ["追问 1", "追问 2"],
  "metadata": {
    "model": "grok-4.20-auto",
    "rollouts_used": ["rollout-0"]
  }
}
```

### 返回（format=openai）

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1714480000,
  "model": "grok-4.20-auto",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "...", "refusal": null},
    "finish_reason": "stop"
  }],
  "usage": null,
  "metadata": {
    "conversation_id": "conv-xxx",
    "response_id": "resp-yyy",
    "x_results": [...],
    "web_results": [...],
    "citations": [...],
    "follow_ups": [...]
  }
}
```

### 典型 prompt 模板

```
# 单轮问答
用 grok_chat 回答：量子纠缠的直观解释是什么？

# 续轮（同 session 自动接上）
grok_chat: "刚才你提到了贝尔不等式，能展开说说吗？"

# 禁用搜索（纯模型推理）
grok_chat(prompt="写一首关于秋天的俳句", disable_search=true)

# 强制开新对话
grok_chat(prompt="你好", conversation_id="")
```

---

## grok_x_search

X 高级搜索（不含 LLM 总结），返回原始结构化推文列表。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | `string` | 必填 | 关键词 |
| `from_users` | `string[]` | `[]` | 只搜索来自这些用户的推文 |
| `exclude_users` | `string[]` | `[]` | 排除这些用户的推文 |
| `since` | `string \| null` | `null` | 起始日期，格式 `YYYY-MM-DD` |
| `until` | `string \| null` | `null` | 截止日期，格式 `YYYY-MM-DD` |
| `within_time` | `string \| null` | `null` | 近期范围，如 `"7d"`、`"24h"`（与 since 互斥） |
| `min_faves` | `int \| null` | `null` | 最少点赞数 |
| `min_retweets` | `int \| null` | `null` | 最少转推数 |
| `min_replies` | `int \| null` | `null` | 最少回复数 |
| `lang` | `string \| null` | `null` | 语言代码，如 `"zh"`、`"en"` |
| `exclude_retweets` | `boolean` | `true` | 排除转推 |
| `exclude_replies` | `boolean` | `false` | 排除回复 |
| `media` | `"images" \| "videos" \| "links" \| null` | `null` | 仅含指定媒体类型的推文 |
| `verified_only` | `boolean` | `false` | 仅蓝 V 认证账号 |
| `raw_query` | `string \| null` | `null` | 追加到查询末尾的原始 X 高级搜索语法 |
| `limit` | `int` | `10` | 最多返回条数（上限 30） |

### 返回

```json
{
  "query_used": "AI since:2026-04-01 min_faves:100 -filter:retweets",
  "result_count": 10,
  "results": [
    {
      "username": "sama",
      "name": "Sam Altman",
      "text": "推文正文...",
      "post_id": "1234567890",
      "post_url": "https://x.com/sama/status/1234567890",
      "create_time": "2026-04-15T10:23:00Z",
      "profile_image_url": "https://...",
      "view_count": 540000,
      "community_note": "",
      "quote": null,
      "parent": null
    }
  ]
}
```

### raw_query 用法示例

`raw_query` 允许追加任意 X 高级搜索语法，覆盖工具参数未支持的过滤器：

```
# 仅含图片的推文，且包含某个话题标签
grok_x_search(query="AI safety", raw_query="#AIrisk filter:images")

# 搜索某人 @mention 的推文（非 from:）
grok_x_search(query="OpenAI", raw_query="to:elonmusk")

# 按对话链搜索（只要根推文）
grok_x_search(query="Claude vs GPT", raw_query="-filter:replies -filter:retweets")

# 精确短语 + 排除词
grok_x_search(query="\"model collapse\"", raw_query="-spam -promo")
```

### 典型 prompt 模板

```
# 过去 7 天高热推文
grok_x_search(
  query="Grok 4",
  within_time="7d",
  min_faves=50,
  lang="zh",
  limit=20
)

# 搜索指定账号在某日期区间的推文
grok_x_search(
  query="AI",
  from_users=["karpathy", "ylecun"],
  since="2026-04-01",
  until="2026-04-30",
  exclude_retweets=true
)

# 仅含图片的蓝 V 推文
grok_x_search(
  query="midjourney",
  media="images",
  verified_only=true,
  min_retweets=10
)
```

---

## grok_web_search

Web 搜索，返回原始结构化结果。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | `string` | 必填 | 搜索词 |
| `recency_days` | `int \| null` | `null` | 限制最近 N 天的结果 |
| `site` | `string \| null` | `null` | 限制搜索域名，如 `"github.com"` |
| `allowed_domains` | `string[] \| null` | `null` | 仅返回这些域名的结果 |
| `excluded_domains` | `string[] \| null` | `null` | 排除这些域名 |
| `raw_query` | `string \| null` | `null` | 追加到查询末尾的原始搜索语法 |
| `limit` | `int` | `10` | 最多返回条数（上限 30） |

### 返回

```json
{
  "query_used": "FastAPI streaming site:github.com",
  "result_count": 8,
  "results": [
    {
      "url": "https://github.com/...",
      "title": "...",
      "preview": "摘要文本..."
    }
  ]
}
```

### 典型 prompt 模板

```
# 搜索 GitHub 上的项目
grok_web_search(query="MCP server Python", site="github.com", limit=10)

# 最近 3 天的新闻
grok_web_search(query="Grok 4 release", recency_days=3)

# 排除特定域名
grok_web_search(
  query="LLM benchmark 2026",
  excluded_domains=["reddit.com", "twitter.com"]
)
```

---

## grok_quota

查询指定模型的剩余配额。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `model` | `string` | `"grok-4.20-auto"` | 模型名 |

### 返回

```json
{
  "model": "grok-4.20-auto",
  "window_size_seconds": 7200,
  "remaining_queries": 18,
  "total_queries": 25
}
```

---

## grok_imagine

通过 Grok chat 通道生成图片。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `prompt` | `string` | 必填 | 图片描述 |
| `n` | `int` | `2` | 生成张数（1-10） |
| `aspect_ratio` | `"1:1" \| "16:9" \| "9:16" \| "3:2" \| "2:3" \| "4:3" \| "3:4"` | `"1:1"` | 图片比例 |
| `return_mode` | `"url" \| "local_path" \| "base64"` | `"url"` | 返回形式 |

`return_mode` 说明：

- `url`：返回 xGate 代理 URL（`/v1/files/proxy`），MCP 客户端无需 cookie 即可访问
- `local_path`：保存到 `data/images/mcp/<session_id>/`，返回本地路径
- `base64`：内嵌 base64 编码（小图可用；大图或多图会使响应体显著膨胀）

### 返回

```json
{
  "session_id": "mcp-1714480000-a3f2b1",
  "moderation": "passed",
  "rephrased_prompt": null,
  "images": [
    {"url": "http://127.0.0.1:8024/v1/files/proxy?url=...", "local_path": null, "base64": null},
    {"url": "http://127.0.0.1:8024/v1/files/proxy?url=...", "local_path": null, "base64": null}
  ]
}
```

### 典型 prompt 模板

```
# 生成两张横版图，通过代理 URL 返回
grok_imagine(prompt="a misty mountain at sunrise, photorealistic", n=2, aspect_ratio="16:9")

# 保存到本地
grok_imagine(prompt="cyberpunk city", n=1, return_mode="local_path")
```

---

## grok_imagine_video

通过 Grok 生成视频（下载后本地缓存）。

> 视频生成通常需要 1-5 分钟，工具会阻塞等待完成。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `prompt` | `string` | 必填 | 视频描述 |
| `aspect_ratio` | `"16:9" \| "9:16" \| "1:1"` | `"16:9"` | 视频比例 |
| `duration_seconds` | `5 \| 10 \| 15` | `5` | 视频时长（秒） |
| `return_mode` | `"url" \| "local_path"` | `"url"` | 返回形式 |

### 返回

```json
{
  "video_url": "http://127.0.0.1:8024/v1/grok/assets/serve/...",
  "local_path": null,
  "duration_seconds": 5
}
```

---

## grok_files_list

列出 Grok 云端文件（实时查询）。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `limit` | `int` | `50` | 每页条数 |
| `offset` | `int` | `0` | 分页偏移 |
| `kind` | `"image" \| "video" \| "all"` | `"all"` | 文件类型过滤 |

### 返回

```json
{
  "items": [
    {
      "file_id": "asset-uuid",
      "name": "image.jpg",
      "kind": "image",
      "size_bytes": 204800,
      "created_at": "2026-04-15T10:00:00Z",
      "url": "http://127.0.0.1:8024/v1/files/proxy?url=..."
    }
  ],
  "total": 128
}
```

---

## grok_files_save_local

将 Grok Files 下载到本地 `data/grok-files/`。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `file_ids` | `string[]` | 必填 | `grok_files_list` 返回的 `file_id` 列表 |

### 返回

```json
{
  "saved": [
    {"file_id": "asset-uuid-1", "local_path": "/app/data/grok-files/image.jpg"}
  ],
  "failed": [
    {"file_id": "asset-uuid-2", "error": "not found in cloud listing"}
  ]
}
```

### 典型用法

```
# 先列出文件，再保存前 5 张图片
files = grok_files_list(kind="image", limit=5)
grok_files_save_local(file_ids=[f["file_id"] for f in files["items"]])
```

---

## grok_files_delete

从 Grok 云端删除文件（不影响已下载到本地的文件）。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `file_ids` | `string[]` | 必填 | `grok_files_list` 返回的 `file_id` 列表 |

### 返回

```json
{
  "deleted": ["asset-uuid-1", "asset-uuid-2"],
  "failed": [
    {"file_id": "asset-uuid-3", "error": "not found on cloud (404/410)"}
  ]
}
```

---

## 错误结构

所有 tool 在遇到 Grok 端错误时返回：

```json
{
  "error": "upstream error message",
  "code": "upstream_error_code"
}
```

常见 code：

| code | 含义 |
|------|------|
| `upstream_unauthorized` | Cookie 失效，需重新导入 cURL |
| `rate_limited` | 配额耗尽，等待窗口重置 |
| `image_moderated` | 图片被内容审核拦截 |
| `timeout` | 请求超时（`grok.timeout_seconds` 控制） |
