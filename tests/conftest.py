"""pytest 全局 fixture：禁用 main.app 的 lifespan，避免后台 worker 拖慢测试。

正式部署时 lifespan 启动 ws_gateway / 文件同步 / FlareSolverr session keeper 等
长生命周期任务；单元测试不需要这些，禁用后单测从 120s 降到 ~1s。
"""

from __future__ import annotations

import os
import pathlib
import sys
from contextlib import asynccontextmanager

# 项目根 cwd（main.py 用 cwd 找 data/config/mini.toml）
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(_REPO_ROOT)

# worktree 内新增的模块（如 accounts.py）尚未 editable-install 到 .venv，
# 需要显式把 worktree 的 src 目录放到 sys.path 最前面，
# 使其优先于主仓库的 editable install 路径。
_WORKTREE_SRC = str(_REPO_ROOT / "src")
if _WORKTREE_SRC not in sys.path:
    sys.path.insert(0, _WORKTREE_SRC)


@asynccontextmanager
async def _noop_lifespan(app):  # noqa: ANN001
    yield


def pytest_configure(config) -> None:  # noqa: ANN001
    # 在任何测试 import main 之前替换 lifespan
    from mini_grok_api import main as main_mod
    main_mod.app.router.lifespan_context = _noop_lifespan


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """每个测试前清空 slowapi limiter 状态。

    /v1/auth/login 限流策略 10/分钟/IP，pytest 跑很多 login 会瞬间超限。
    TestClient 默认所有请求来自同一 IP（testclient），不清的话第 11 次开始 429。
    """
    from mini_grok_api import main as main_mod
    main_mod.limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _isolate_settings_disk_writes(monkeypatch):
    """BUG-D 防护：阻止任何测试经由 /admin/config 等端点把开发者的 prod
    `data/config/mini.toml` 改写。settings_store.update() 改为只更新内存
    `_settings`，不调用 save_settings。

    SAST round 4 (P2-2) 补强：测试可能直接写 `_ss._settings = ...`（如
    `_override_settings` helper 用类 setUpClass 改 api_key 后不还原），
    fixture 退出时强制把 `_settings` 还原到本测试开始前的值，避免跨测试污染。

    任何依赖「update 后下次 get() 能读到新值」的测试仍然 work（内存更新到位）；
    只是磁盘上的 mini.toml 不会被污染。
    """
    from dataclasses import replace as _dc_replace
    from mini_grok_api import main as main_mod
    _ss = main_mod.settings_store  # singleton 在 main.py 创建
    _original_settings = _ss.get()  # 记录入口快照

    def _in_memory_update(**kwargs):
        new = _dc_replace(_ss.get(), **kwargs)
        _ss._settings = new  # type: ignore[attr-defined]
        return new

    monkeypatch.setattr(_ss, "update", _in_memory_update)
    yield
    # P2-2 还原：即使测试代码绕过 update() 直接写 _settings，也强制恢复
    _ss._settings = _original_settings  # type: ignore[attr-defined]
