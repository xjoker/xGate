# TODO

未排期 / 待规划的工作项。按优先级排列。

---

## P0

（暂无）

## P1

（暂无）

## P2

- `service_tier` / `store` 等 OpenAI 字段做实际持久化（**已决策跳过**：实现需 ~150 行，OpenAI 客户端实际几乎不传，收益接近零；pydantic schema 已接受字段不会 422，只是不存盘。Yuki memory `1e8906b4`）

## 已完成（历史记录）

Phase 3 backlog 全部清空（详见 CHANGELOG v0.3.1 ~ v0.3.3）：

- ✓ `/v1/auth/login` 限流（slowapi 10/min/IP）
- ✓ priority/weight 在线编辑（账号 edit modal）
- ✓ `X-Account-Label` 客户端 header（9 个用户面 endpoint 全覆盖）
- ✓ conversation_id sticky binding（TTL 7d + 后台 cleanup）
- ✓ GitHub Actions 升级到最新 major（Node 24）
- ✓ `/v1/videos/status` POST 化（CSRF 加固）
- ✓ per-account image quota
- ✓ CI 接入 `pip-audit --strict`，0 已知 CVE

P2 tech debt（详见 CHANGELOG v0.3.9）：

- ✓ 反向代理 `X-Forwarded-For` 感知限流（`server.trust_x_forwarded_for`，v0.3.9）
- ✓ `_IMAGE_QUOTA_MODEL_HINT` 持久化到 mini.toml（`grok.image_quota_model_name`，v0.3.9）
