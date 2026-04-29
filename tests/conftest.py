"""pytest 全局 fixture：禁用 main.app 的 lifespan，避免后台 worker 拖慢测试。

正式部署时 lifespan 启动 ws_gateway / 文件同步 / FlareSolverr session keeper 等
长生命周期任务；单元测试不需要这些，禁用后单测从 120s 降到 ~1s。
"""

from __future__ import annotations

import os
import pathlib
from contextlib import asynccontextmanager

# 项目根 cwd（main.py 用 cwd 找 data/config/mini.toml）
os.chdir(pathlib.Path(__file__).resolve().parent.parent)


@asynccontextmanager
async def _noop_lifespan(app):  # noqa: ANN001
    yield


def pytest_configure(config) -> None:  # noqa: ANN001
    # 在任何测试 import main 之前替换 lifespan
    from mini_grok_api import main as main_mod
    main_mod.app.router.lifespan_context = _noop_lifespan
