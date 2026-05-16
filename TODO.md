# TODO

未排期 / 待规划的工作项。按优先级排列。

---

## P0

（暂无）

## P1

（暂无）

## P2

- `service_tier` / `store` 等 OpenAI 字段做实际持久化（目前仅校验吞掉；`metadata` 已被 conversation_id sticky binding 消费）
- 反向代理部署时 `slowapi.util.get_remote_address` 拿到的是代理 IP，所有真实客户端会被算成同一 IP — 接入 `X-Forwarded-For` 支持需要配套可信代理 IP 白名单
- `_IMAGE_QUOTA_MODEL_HINT` 当前只存在进程内存，重启后第一轮 poll 会重新跑 4-candidate 探测。可选持久化到 settings 或 mini.toml

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
