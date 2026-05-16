# xGate Examples

可以直接复制运行的代码片段，覆盖最常见的客户端接入与多账号场景。

## 目录

| 文件 | 演示 |
|---|---|
| [`openai_basic.py`](./openai_basic.py) | OpenAI Python SDK 接入 xGate 跑 chat/image |
| [`openai_streaming.py`](./openai_streaming.py) | 流式 chat completions（SSE）|
| [`x_account_label.py`](./x_account_label.py) | 用 `X-Account-Label` header 强制指定账号 |
| [`sticky_binding.py`](./sticky_binding.py) | 用 `metadata.conversation_id` 让多轮对话固定账号 |
| [`multi_account_admin.py`](./multi_account_admin.py) | 通过 admin API 批量管理账号（导入 cURL/启用/禁用/删除/查 quota）|
| [`curl_quickstart.sh`](./curl_quickstart.sh) | 纯 cURL 一键体验所有核心端点 |

## 通用前提

1. xGate 在 `http://127.0.0.1:8024` 运行
2. `mini.toml` 已配置真实 `api_key` 和至少一个 grok cookie
3. Python 示例需 `pip install openai requests`

## 运行单个示例

```bash
export XGATE_API_KEY="你的-api-key"
python examples/openai_basic.py
```
