# Contributing to xGate

Thanks for your interest in xGate.

## Issues & PRs

- 提 Issue 前请先搜索是否已有类似讨论
- 报 Bug 请贴：xGate 版本、FlareSolverr 版本、Docker / 本地运行方式、复现步骤、`docker logs` 节选
- 提 PR 前请确认 `uv run pytest tests/` 通过

## 开发流程

```bash
git clone https://github.com/xjoker/xGate.git
cd xGate
uv sync
cp data/config/mini.toml.example data/config/mini.toml   # 填 cookie / proxy / flaresolverr
uv run xgate
```

修改 Web UI（`src/mini_grok_api/static/index.html`）无需重建镜像，刷新浏览器即可。Python 改动重启服务。

## 风格

- Python 3.13+，强类型注解，`ruff` 校验，line-length=100
- 日志走标准 `logging`（`logger = logging.getLogger(__name__)`），禁止 `print()`
- 所有外部 IO 走 async（`aiohttp` / `curl_cffi.requests.AsyncSession`）

## 发布流程（maintainer 用）

1. 打 tag 触发 CI 构建并推送到 GHCR：
   ```bash
   git tag v0.x.y && git push origin v0.x.y
   ```
2. **首次发布后**：进入 `https://github.com/xjoker/xGate/pkgs/container/xgate` → Package settings → Change visibility → **Public**，否则用户需要登录才能 `docker pull`。

## 安全提交清单

- [ ] 没有硬编码 API key、cookie、proxy 凭据
- [ ] 没有引入新的环境变量魔法（统一走 `data/config/mini.toml`）
- [ ] 改动不依赖作者本地路径或机器特定 IP
