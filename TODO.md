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
