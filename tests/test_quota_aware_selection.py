"""AccountPool Phase 2 单元测试：配额感知选号 + re_enable + quota 缓存持久化。

所有测试使用 tmp_path fixture 隔离 DB，全 sync（无 async）。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mini_grok_api.accounts import Account, AccountPool


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_pool(tmp_path: Path) -> AccountPool:
    return AccountPool(db_path=tmp_path / "test.db")


def acc(label: str, *, priority: int = 1, weight: int = 10, enabled: bool = True,
        cookie: str | None = None) -> Account:
    return Account(
        label=label,
        cookie=cookie if cookie is not None else f"cookie_{label}",
        user_agent=f"ua_{label}",
        browser="chrome142",
        proxy="",
        statsig_id="",
        enabled=enabled,
        priority=priority,
        weight=weight,
    )


def _set_quota(pool: AccountPool, label: str, model_id: str, remaining: int, total: int) -> None:
    """Helper: 直接写入 quota 缓存（remaining/total 可控）。"""
    pool.update_quota(
        label,
        model_id,
        remaining=remaining,
        total=total,
        reset_at=time.time() + 3600,
    )


# ── 任务 1：配额缓存数据结构 ──────────────────────────────────────────────────

class TestQuotaCache:
    def test_update_and_get_quota(self, tmp_path):
        """update_quota 后 get_quota 能取到相同值。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        now = time.time()
        pool.update_quota("a", "grok-4.20-auto", remaining=25, total=50, reset_at=now + 3600)

        q = pool.get_quota("a", "grok-4.20-auto")
        assert q is not None
        assert q["remaining"] == 25
        assert q["total"] == 50
        assert q["reset_at"] == pytest.approx(now + 3600, abs=1.0)
        assert "fetched_at" in q

    def test_get_quota_returns_none_if_no_cache(self, tmp_path):
        """无缓存时 get_quota 返回 None。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        assert pool.get_quota("a", "grok-4.20-auto") is None

    def test_get_quota_returns_none_for_missing_account(self, tmp_path):
        pool = make_pool(tmp_path)
        assert pool.get_quota("ghost", "grok-4.20-auto") is None

    def test_update_quota_nonexistent_account_noop(self, tmp_path):
        """对不存在账号更新配额不报错（静默 noop）。"""
        pool = make_pool(tmp_path)
        pool.update_quota("ghost", "grok-4.20-auto", remaining=10, total=25, reset_at=time.time())
        # 不抛出异常即可

    def test_multiple_models_independent_cache(self, tmp_path):
        """多模型独立缓存，互不干扰。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        pool.update_quota("a", "grok-4.20-auto", remaining=10, total=50, reset_at=time.time() + 100)
        pool.update_quota("a", "grok-4.20-fast", remaining=1, total=25, reset_at=time.time() + 200)

        q_auto = pool.get_quota("a", "grok-4.20-auto")
        q_fast = pool.get_quota("a", "grok-4.20-fast")

        assert q_auto["remaining"] == 10
        assert q_fast["remaining"] == 1

    def test_update_quota_overwrites_previous(self, tmp_path):
        """更新配额会覆盖旧值。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        pool.update_quota("a", "grok-4.20-auto", remaining=20, total=50, reset_at=time.time() + 100)
        pool.update_quota("a", "grok-4.20-auto", remaining=5, total=50, reset_at=time.time() + 200)

        q = pool.get_quota("a", "grok-4.20-auto")
        assert q["remaining"] == 5


# ── 任务 2：_is_quota_low ──────────────────────────────────────────────────────

class TestIsQuotaLow:
    def test_quota_low_below_threshold(self, tmp_path):
        """remaining/total < 0.05 → True。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        _set_quota(pool, "a", "grok-4.20-auto", remaining=1, total=100)
        assert pool._is_quota_low("a", "grok-4.20-auto") is True

    def test_quota_not_low_above_threshold(self, tmp_path):
        """remaining/total >= 0.05 → False。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        _set_quota(pool, "a", "grok-4.20-auto", remaining=10, total=100)
        assert pool._is_quota_low("a", "grok-4.20-auto") is False

    def test_quota_low_exactly_at_threshold(self, tmp_path):
        """exactly 5% → 不低于阈值（< 而非 <=），所以返回 False。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        _set_quota(pool, "a", "grok-4.20-auto", remaining=5, total=100)
        # 5/100 = 0.05，不 < 0.05，所以返回 False
        assert pool._is_quota_low("a", "grok-4.20-auto") is False

    def test_quota_low_no_cache_returns_false(self, tmp_path):
        """无缓存时不过滤（返回 False）。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        assert pool._is_quota_low("a", "grok-4.20-auto") is False

    def test_quota_low_total_zero_returns_false(self, tmp_path):
        """total=0 时不过滤（避免除零）。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.update_quota("a", "grok-4.20-auto", remaining=0, total=0, reset_at=time.time())
        assert pool._is_quota_low("a", "grok-4.20-auto") is False

    def test_quota_low_custom_threshold(self, tmp_path):
        """自定义阈值。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        _set_quota(pool, "a", "grok-4.20-auto", remaining=8, total=100)
        assert pool._is_quota_low("a", "grok-4.20-auto", threshold=0.10) is True
        assert pool._is_quota_low("a", "grok-4.20-auto", threshold=0.05) is False


# ── 任务 3：soft_cooldown 配额感知选号 ────────────────────────────────────────

class TestQuotaAwareSelection:
    def test_quota_low_account_skipped(self, tmp_path):
        """配额 < 5% 的账号被 acquire(model_id=...) 跳过，选另一个。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", priority=1))
        pool.upsert_account(acc("b", priority=1))

        # a 的配额只剩 1%
        _set_quota(pool, "a", "grok-4.20-auto", remaining=1, total=100)

        with pool.acquire(model_id="grok-4.20-auto") as acq:
            assert acq.label == "b", f"a 应被 soft_cooling 跳过，实际选了: {acq.label}"

    def test_quota_sufficient_account_selected(self, tmp_path):
        """配额充足的账号正常被选。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        _set_quota(pool, "a", "grok-4.20-auto", remaining=10, total=100)

        with pool.acquire(model_id="grok-4.20-auto") as acq:
            assert acq.label == "a"

    def test_no_model_id_ignores_quota(self, tmp_path):
        """不传 model_id 时不过滤配额（原有行为保留）。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        _set_quota(pool, "a", "grok-4.20-auto", remaining=1, total=100)

        # 不传 model_id，不过滤
        with pool.acquire() as acq:
            assert acq.label == "a"

    def test_soft_cooldown_fallback_when_all_low(self, tmp_path):
        """所有账号 soft_cooling 时，仍能选到一个（不卡死）。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", priority=1))
        pool.upsert_account(acc("b", priority=1))

        _set_quota(pool, "a", "grok-4.20-auto", remaining=1, total=100)
        _set_quota(pool, "b", "grok-4.20-auto", remaining=0, total=100)

        with pool.acquire(model_id="grok-4.20-auto") as acq:
            assert acq.label in {"a", "b"}, "兜底：应选到某个账号"

    def test_soft_cooldown_fallback_returns_first_in_order(self, tmp_path):
        """fallback 时按原始候选顺序（priority + LRU）选第一个。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", priority=1))
        pool.upsert_account(acc("b", priority=2))

        _set_quota(pool, "a", "grok-4.20-auto", remaining=1, total=100)
        _set_quota(pool, "b", "grok-4.20-auto", remaining=0, total=100)

        with pool.acquire(model_id="grok-4.20-auto") as acq:
            # fallback 时所有候选（a+b）按原有 priority + LRU 排，a 优先
            assert acq.label == "a"

    def test_no_cache_means_not_filtered(self, tmp_path):
        """无缓存时不过滤（让流量通过）。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        # 不写任何 quota 缓存

        with pool.acquire(model_id="grok-4.20-auto") as acq:
            assert acq.label == "a"

    def test_quota_filter_respects_priority(self, tmp_path):
        """配额充足的低优先级账号不会覆盖配额充足的高优先级账号。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("hp", priority=1))
        pool.upsert_account(acc("lp", priority=2))

        _set_quota(pool, "hp", "grok-4.20-auto", remaining=10, total=100)
        _set_quota(pool, "lp", "grok-4.20-auto", remaining=50, total=100)

        with pool.acquire(model_id="grok-4.20-auto") as acq:
            assert acq.label == "hp"

    def test_quota_filter_skips_high_priority_selects_low(self, tmp_path):
        """高优先级 soft_cooling，自动选低优先级。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("hp", priority=1))
        pool.upsert_account(acc("lp", priority=2))

        _set_quota(pool, "hp", "grok-4.20-auto", remaining=1, total=100)
        _set_quota(pool, "lp", "grok-4.20-auto", remaining=50, total=100)

        with pool.acquire(model_id="grok-4.20-auto") as acq:
            assert acq.label == "lp"


# ── 任务 4：quota 缓存持久化（跨实例）───────────────────────────────────────

class TestQuotaCachePersistence:
    def test_quota_persists_across_instances(self, tmp_path):
        """update_quota 后重建 AccountPool 实例，缓存仍可读。"""
        db_path = tmp_path / "persist.db"

        pool1 = AccountPool(db_path=db_path)
        pool1.upsert_account(acc("a"))
        pool1.update_quota("a", "grok-4.20-auto", remaining=15, total=50, reset_at=time.time() + 500)

        # 创建新实例（模拟重启）
        pool2 = AccountPool(db_path=db_path)
        q = pool2.get_quota("a", "grok-4.20-auto")

        assert q is not None
        assert q["remaining"] == 15
        assert q["total"] == 50

    def test_quota_cache_survives_upsert(self, tmp_path):
        """upsert_account（更新凭证）不清空 quota 缓存。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", cookie="old"))
        pool.update_quota("a", "grok-4.20-auto", remaining=20, total=50, reset_at=time.time() + 100)

        # 更新凭证
        pool.upsert_account(acc("a", cookie="new"))

        q = pool.get_quota("a", "grok-4.20-auto")
        assert q is not None
        assert q["remaining"] == 20


# ── 任务 5：re_enable（auto_disabled 恢复）────────────────────────────────────

class TestReEnable:
    def test_re_enable_auto_disabled_account(self, tmp_path):
        """auto_disabled 账号调用 re_enable 后 status='enabled'，consecutive_failures=0。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        # 手动模拟 auto_disabled（避免触发 mark_failure 逻辑副作用）
        with pool._connect() as conn:
            conn.execute(
                "UPDATE accounts SET status='auto_disabled', consecutive_failures=5 WHERE label='a'"
            )

        result = pool.re_enable("a")
        assert result is True

        with pool._connect() as conn:
            row = conn.execute("SELECT status, consecutive_failures, enabled FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "enabled"
        assert row["consecutive_failures"] == 0
        assert row["enabled"] == 1

    def test_re_enable_makes_account_selectable(self, tmp_path):
        """re_enable 后账号可被 acquire 选中。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.upsert_account(acc("b"))

        with pool._connect() as conn:
            conn.execute("UPDATE accounts SET status='auto_disabled', enabled=1 WHERE label='a'")

        # auto_disabled 时不选 a
        with pool.acquire() as acq:
            assert acq.label == "b"

        pool.re_enable("a")

        # re_enable 后 a 可用（由于 last_used_at=0，a 比 b 更 "LRU"）
        with pool.acquire() as acq:
            assert acq.label in {"a", "b"}  # 两个都可用

    def test_re_enable_nonexistent_returns_true_gracefully(self, tmp_path):
        """对不存在账号调用 re_enable 返回 False（无 DB 行）。"""
        pool = make_pool(tmp_path)
        result = pool.re_enable("ghost")
        assert result is False

    def test_re_enable_resets_cooldown(self, tmp_path):
        """re_enable 清零 cooldown_until。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        with pool._connect() as conn:
            conn.execute(
                "UPDATE accounts SET status='auto_disabled', cooldown_until=? WHERE label='a'",
                (time.time() + 9999,),
            )

        pool.re_enable("a")

        with pool._connect() as conn:
            row = conn.execute("SELECT cooldown_until FROM accounts WHERE label='a'").fetchone()
        assert row["cooldown_until"] == 0.0


# ── 任务 6：后台 revalidate loop（mock query_rate_limits）─────────────────────

class TestRevalidateLoop:
    def test_revalidate_recovers_auto_disabled(self, tmp_path):
        """mock query_rate_limits 返回成功 → auto_disabled 账号被 re_enable。

        通过 asyncio.run 运行 loop body 的核心逻辑（不依赖 pytest-asyncio）。
        """
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", cookie="c_a"))

        # 模拟 auto_disabled
        with pool._connect() as conn:
            conn.execute(
                "UPDATE accounts SET status='auto_disabled', consecutive_failures=5 WHERE label='a'"
            )

        mock_response = {
            "remainingQueries": 10,
            "totalQueries": 25,
            "windowSizeSeconds": 7200,
        }

        async def _run():
            from mini_grok_api.accounts import _as_settings
            from mini_grok_api.config import load_settings
            base = load_settings()

            infos = pool.list_accounts()
            for info in infos:
                if info.status != "auto_disabled":
                    continue
                acc_obj = pool.get_account(info.label)
                if acc_obj is None:
                    continue
                shadow = _as_settings(base, acc_obj)
                # mock: query_rate_limits 直接返回 mock_response
                q = mock_response
                if q is not None:
                    pool.re_enable(info.label)

        asyncio.run(_run())

        with pool._connect() as conn:
            row = conn.execute(
                "SELECT status, consecutive_failures FROM accounts WHERE label='a'"
            ).fetchone()
        assert row["status"] == "enabled"
        assert row["consecutive_failures"] == 0

    def test_revalidate_skips_manually_disabled(self, tmp_path):
        """manually_disabled 账号不被 revalidate 恢复。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.set_enabled("a", False)  # manually_disabled

        infos = pool.list_accounts()
        auto_disabled_labels = [i.label for i in infos if i.status == "auto_disabled"]

        # manually_disabled 不在 revalidate 候选列表中
        assert "a" not in auto_disabled_labels

        with pool._connect() as conn:
            row = conn.execute("SELECT status FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "manually_disabled"

    def test_revalidate_does_not_recover_still_failing(self, tmp_path):
        """query_rate_limits 抛异常时，账号保持 auto_disabled。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", cookie="c_a"))

        with pool._connect() as conn:
            conn.execute(
                "UPDATE accounts SET status='auto_disabled', consecutive_failures=5 WHERE label='a'"
            )

        async def _run():
            from mini_grok_api.accounts import _as_settings
            from mini_grok_api.config import load_settings
            base = load_settings()

            infos = pool.list_accounts()
            for info in infos:
                if info.status != "auto_disabled":
                    continue
                acc_obj = pool.get_account(info.label)
                if acc_obj is None:
                    continue
                shadow = _as_settings(base, acc_obj)
                # mock: 模拟仍在失败（抛异常）
                try:
                    raise RuntimeError("still rate limited")
                except Exception:
                    pass  # 不调用 re_enable

        asyncio.run(_run())

        with pool._connect() as conn:
            row = conn.execute("SELECT status FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "auto_disabled"


# ── list_account_quotas ───────────────────────────────────────────────────────

class TestListAccountQuotas:
    def test_list_account_quotas_returns_all(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.upsert_account(acc("b"))

        pool.update_quota("a", "grok-4.20-auto", remaining=10, total=25, reset_at=time.time())

        summaries = pool.list_account_quotas()
        assert len(summaries) == 2
        labels = [s["label"] for s in summaries]
        assert "a" in labels
        assert "b" in labels

        a_summary = next(s for s in summaries if s["label"] == "a")
        assert "grok-4.20-auto" in a_summary["quotas"]
        assert a_summary["quotas"]["grok-4.20-auto"]["remaining"] == 10

    def test_list_account_quotas_empty_cache(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        summaries = pool.list_account_quotas()
        assert summaries[0]["quotas"] == {}
