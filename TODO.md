# TODO

未排期 / 待规划的工作项。按优先级排列。

---

## P0

（暂无）

## P1

### 安全：API Key 鉴权改用 HttpOnly Cookie（方案 C）

**问题**

`/v1/grok/assets/download` 当前支持 `?api_key=xxx` 双通道鉴权，前端把 key 拼进 `<img src>` 用于缩略图与 chat-imagine 结果展示。涉及：

- `src/mini_grok_api/main.py:2063` — endpoint 接受 `api_key: str = ""` query 参数
- `src/mini_grok_api/static/index.html:1792` — chat-imagine 渲染 `<img src="...?api_key=...">`
- `src/mini_grok_api/static/index.html:3321` — 文件缩略图 `<img src="...&api_key=...">`

风险路径：
- uvicorn / 反向代理 access log 记录完整 query string
- 截图 / 录屏 / DevTools Network 面板明文显示
- 浏览器扩展可读 DOM 与 Network
- 浏览器历史与缓存键可能保留

**目标方案**：HttpOnly Cookie 鉴权（方案 C）

工作内容：
- 新增 `/login` 与 `/logout` 端点；提交 api_key 后 `Set-Cookie: xgate_session=<token>; HttpOnly; Secure; SameSite=Strict; Path=/`
- 服务端 token：HMAC-SHA256(api_key, random_nonce + expires)，存内存 / SQLite 短表
- 现有 `_require_api_key` 依赖增加 cookie 通道，与 `Authorization: Bearer` 并存
- 前端登录态：从 sessionStorage 切到完全依赖 cookie，移除 `getKey()` 在 URL 中的拼接
- 添加 CSRF token（Double Submit Cookie 或 Origin/Referer 校验）
- 移除 `download` endpoint 的 `?api_key=` 参数支持
- 添加 logout/会话过期处理与前端 401 自动跳登录页

工作量预估：1-2 天

**临时止血（如方案 C 上线前需要）**

- uvicorn 启动时挂自定义 access log filter，把 `api_key=xxx` 替换成 `api_key=***`（15 分钟）
- 此为缓解非根治，方案 C 落地后可移除

---

## P2

（暂无）
