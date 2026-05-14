"""Phase 2 per-account 统计单元测试。

覆盖：
- Monitor 分桶记录与全局聚合
- DB schema 迁移（account_label 列）
- log_chat / log_image 持久化 account_label
- 旧数据（无 account_label）兼容读取
- /v1/logs?account_label= 过滤
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport


# ── Monitor 测试 ───────────────────────────────────────────────────────────────

class TestMonitorPerAccount:
    def test_records_per_account(self):
        from mini_grok_api.monitor import Monitor
        m = Monitor()
        m.record_start("acc-a")
        m.record_success("acc-a", 200)
        m.record_start("acc-b")
        m.record_failure("acc-b", 502, "boom")

        snap = m.snapshot()
        assert "acc-a" in snap.per_account
        assert "acc-b" in snap.per_account

        a = snap.per_account["acc-a"]
        assert a.total_requests == 1
        assert a.success_count == 1
        assert a.failure_count == 0

        b = snap.per_account["acc-b"]
        assert b.total_requests == 1
        assert b.success_count == 0
        assert b.failure_count == 1

    def test_snapshot_aggregates_global(self):
        from mini_grok_api.monitor import Monitor
        m = Monitor()
        m.record_start("acc-a")
        m.record_success("acc-a", 200)
        m.record_start("acc-b")
        m.record_failure("acc-b", 502, "boom")

        snap = m.snapshot()
        assert snap.total_requests == 2
        assert snap.success_count == 1
        assert snap.failure_count == 1

    def test_empty_label_bucket(self):
        """空字符串 label 也正常落桶（兼容旧调用点）。"""
        from mini_grok_api.monitor import Monitor
        m = Monitor()
        m.record_start()          # default ""
        m.record_success()        # default ""

        snap = m.snapshot()
        assert snap.total_requests == 1
        assert snap.success_count == 1
        assert "" in snap.per_account

    def test_cloudflare_flag_is_global(self):
        """cloudflare_challenge 标志为全局（任意 account 触发即置位）。"""
        from mini_grok_api.monitor import Monitor
        m = Monitor()
        m.record_failure("acc-a", 403, "cf block", cloudflare=True)
        snap = m.snapshot()
        assert snap.cloudflare_challenge is True

    def test_per_account_is_deep_copy(self):
        """snapshot() 返回的 per_account 是独立拷贝，后续写入不影响已有 snapshot。"""
        from mini_grok_api.monitor import Monitor
        m = Monitor()
        m.record_start("x")
        snap1 = m.snapshot()
        m.record_success("x")
        snap2 = m.snapshot()
        # snap1 的 x.success_count 不应被 snap2 的记录影响
        assert snap1.per_account["x"].success_count == 0
        assert snap2.per_account["x"].success_count == 1

    def test_smoke_scenario(self):
        """任务说明中的 smoke 场景。"""
        from mini_grok_api.monitor import Monitor
        m = Monitor()
        m.record_start("acc-a")
        m.record_success("acc-a", 200)
        m.record_start("acc-b")
        m.record_failure("acc-b", 502, "boom")
        snap = m.snapshot()
        assert snap.total_requests == 2
        assert snap.success_count == 1
        assert snap.failure_count == 1
        assert set(snap.per_account.keys()) == {"acc-a", "acc-b"}


# ── DB 迁移 / log 持久化测试 ──────────────────────────────────────────────────

class TestDBAccountLabel:
    def test_add_account_label_column(self, tmp_path: Path):
        """_init 后 chat_logs 应有 account_label 列。"""
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        with db._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_logs)").fetchall()}
        assert "account_label" in cols

    def test_all_four_tables_have_column(self, tmp_path: Path):
        """四张表（chat/image/video/mcp）都应迁移成功。"""
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        for tbl in ("chat_logs", "image_logs", "video_logs", "mcp_logs"):
            with db._connect() as conn:
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            assert "account_label" in cols, f"{tbl} missing account_label"

    def test_log_chat_with_account_label(self, tmp_path: Path):
        """log_chat 写入 account_label，读回时正确。"""
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        db.log_chat(
            request_id="req-001",
            model="grok-4",
            prompt="hello",
            response="world",
            status="success",
            duration_ms=100,
            account_label="acc-x",
        )
        with db._connect() as conn:
            row = conn.execute("SELECT account_label FROM chat_logs WHERE request_id='req-001'").fetchone()
        assert row is not None
        assert row["account_label"] == "acc-x"

    def test_log_image_with_account_label(self, tmp_path: Path):
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        db.log_image(
            request_id="req-img-001",
            model="grok-imagine",
            prompt="a cat",
            image_count=1,
            status="success",
            duration_ms=200,
            account_label="acc-img",
        )
        with db._connect() as conn:
            row = conn.execute("SELECT account_label FROM image_logs WHERE request_id='req-img-001'").fetchone()
        assert row is not None
        assert row["account_label"] == "acc-img"

    def test_log_video_with_account_label(self, tmp_path: Path):
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        db.log_video(
            request_id="req-vid-001",
            model="grok-video",
            prompt="a dog",
            status="success",
            duration_ms=300,
            account_label="acc-vid",
        )
        with db._connect() as conn:
            row = conn.execute("SELECT account_label FROM video_logs WHERE request_id='req-vid-001'").fetchone()
        assert row is not None
        assert row["account_label"] == "acc-vid"

    def test_log_mcp_with_account_label(self, tmp_path: Path):
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        db.log_mcp(
            request_id="req-mcp-001",
            tool="grok_chat",
            status="success",
            duration_ms=50,
            account_label="acc-mcp",
        )
        with db._connect() as conn:
            row = conn.execute("SELECT account_label FROM mcp_logs WHERE request_id='req-mcp-001'").fetchone()
        assert row is not None
        assert row["account_label"] == "acc-mcp"

    def test_old_logs_have_empty_account_label(self, tmp_path: Path):
        """模拟旧数据（直接 INSERT 不带 account_label）→ 仍可读，值为 '' (DEFAULT '')。"""
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        # 直接 INSERT 不带 account_label（模拟迁移前旧数据）
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO chat_logs (request_id,created_at,model,prompt,status,duration_ms)"
                " VALUES (?,?,?,?,?,?)",
                ("old-req", time.time(), "grok-4", "old prompt", "success", 50),
            )
        # 读取：account_label 应为默认值 ''
        with db._connect() as conn:
            row = conn.execute("SELECT account_label FROM chat_logs WHERE request_id='old-req'").fetchone()
        assert row is not None
        assert row["account_label"] == ""

    def test_query_filter_by_account_label(self, tmp_path: Path):
        """log_db.query(account_label='acc-x') 只返回对应行。"""
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        db.log_chat(
            request_id="r1", model="m", prompt="p",
            status="success", duration_ms=10, account_label="acc-x",
        )
        db.log_chat(
            request_id="r2", model="m", prompt="p",
            status="success", duration_ms=10, account_label="acc-y",
        )
        db.log_chat(
            request_id="r3", model="m", prompt="p",
            status="success", duration_ms=10, account_label="acc-x",
        )

        rows, total = db.query(log_type="chat", account_label="acc-x")
        assert total == 2
        assert len(rows) == 2
        for r in rows:
            assert r["account_label"] == "acc-x"

    def test_query_no_filter_returns_all(self, tmp_path: Path):
        """account_label='' 时不过滤（兼容原行为）。"""
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        for i in range(3):
            db.log_chat(
                request_id=f"r{i}", model="m", prompt="p",
                status="success", duration_ms=10, account_label=f"acc-{i}",
            )
        rows, total = db.query(log_type="chat", account_label="")
        assert total == 3

    def test_migration_is_idempotent(self, tmp_path: Path):
        """多次调用 _init 不报错（IF NOT EXISTS + PRAGMA 保护）。"""
        from mini_grok_api.db import LogDB
        db = LogDB(path=tmp_path / "test.db")
        db._init()  # 再跑一次，不应抛出
        with db._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_logs)").fetchall()}
        assert "account_label" in cols


# ── /v1/logs endpoint 过滤测试 ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_logs_endpoint_filter_by_account(tmp_path: Path):
    """GET /v1/logs?account_label=acc-x 仅返回对应行。"""
    from dataclasses import replace
    from mini_grok_api import main as main_mod
    from mini_grok_api.db import LogDB

    # 注入一个独立 DB 实例到 main 模块
    test_db = LogDB(path=tmp_path / "endpoint_test.db")
    original_db = main_mod.log_db
    main_mod.log_db = test_db

    # 保存并替换 settings（使用 replace 保留所有其他字段）
    original_settings = main_mod.settings_store.get()
    test_settings = replace(original_settings, api_key="test-key-logs")
    main_mod.settings_store._settings = test_settings  # type: ignore[attr-defined]

    try:
        test_db.log_chat(
            request_id="e1", model="m", prompt="p",
            status="success", duration_ms=10, account_label="acc-x",
        )
        test_db.log_chat(
            request_id="e2", model="m", prompt="p",
            status="success", duration_ms=10, account_label="acc-y",
        )
        test_db.log_chat(
            request_id="e3", model="m", prompt="p",
            status="success", duration_ms=10, account_label="acc-x",
        )

        transport = ASGITransport(app=main_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/v1/logs",
                params={"log_type": "chat", "account_label": "acc-x"},
                headers={"Authorization": "Bearer test-key-logs"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        for row in data["data"]:
            assert row["account_label"] == "acc-x"
    finally:
        main_mod.log_db = original_db
        main_mod.settings_store._settings = original_settings  # type: ignore[attr-defined]
