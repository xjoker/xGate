"""AccountPool 单元测试。

所有测试使用 tmp_path fixture 隔离 DB，全 sync（无 async）。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from mini_grok_api.accounts import Account, AccountPool, AccountInfo


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


# ── 选号基础 ──────────────────────────────────────────────────────────────────

class TestAcquireBasic:
    def test_acquire_single_account(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        with pool.acquire() as acq:
            assert acq.label == "a"

    def test_acquire_lru_two_accounts_same_priority(self, tmp_path):
        """同 priority，LRU（last_used_at 升序）选第一次未使用的。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", priority=1))
        pool.upsert_account(acc("b", priority=1))

        labels = []
        with pool.acquire() as acq:
            labels.append(acq.label)
        with pool.acquire() as acq:
            labels.append(acq.label)

        # 两次应该选到不同账号（LRU 轮换）
        assert labels[0] != labels[1], f"期望 LRU 轮换，实际: {labels}"
        assert set(labels) == {"a", "b"}

    def test_acquire_priority_ordering(self, tmp_path):
        """priority 小的账号优先。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("low", priority=2))
        pool.upsert_account(acc("high", priority=1))

        with pool.acquire() as acq:
            assert acq.label == "high"

    def test_acquire_settings_has_account_cookie(self, tmp_path):
        """acq.settings.grok_cookie 应与所选账号的 cookie 一致。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", cookie="my_special_cookie"))

        with pool.acquire() as acq:
            assert acq.settings.grok_cookie == "my_special_cookie"

    def test_acquire_empty_pool_returns_fallback(self, tmp_path):
        """DB 无账号时，返回 _settings_fallback，不抛异常。"""
        pool = make_pool(tmp_path)
        with pool.acquire() as acq:
            assert acq.label == "_settings_fallback"

    def test_force_label(self, tmp_path):
        """force_label 强制选指定账号。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", priority=1))
        pool.upsert_account(acc("b", priority=2))

        with pool.acquire(force_label="b") as acq:
            assert acq.label == "b"


# ── Cooldown / mark_failure ───────────────────────────────────────────────────

class TestCooldown:
    def test_cooling_account_skipped(self, tmp_path):
        """mark_failure 后账号进入 cooling，acquire 跳过。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.upsert_account(acc("b"))

        pool.mark_failure("a", "image_rate_limited")

        with pool.acquire() as acq:
            assert acq.label == "b", "a 冷却中，应选 b"

    def test_cooldown_expired_account_becomes_available(self, tmp_path, monkeypatch):
        """cooldown 过期后账号可再次被选。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        # 模拟 cooldown_until = 过去，即已过期
        now = time.time()
        with pool._connect() as conn:
            conn.execute(
                "UPDATE accounts SET status='cooling', cooldown_until=? WHERE label='a'",
                (now - 1.0,),
            )

        with pool.acquire() as acq:
            assert acq.label == "a"

    def test_cooldown_not_expired_skipped(self, tmp_path):
        """cooldown 未过期时账号不被选。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("only"))

        # 手动设置很长的冷却
        with pool._connect() as conn:
            conn.execute(
                "UPDATE accounts SET status='cooling', cooldown_until=? WHERE label='only'",
                (time.time() + 9999,),
            )

        with pool.acquire() as acq:
            assert acq.label == "_settings_fallback"

    def test_rate_limit_cooldown_60s(self, tmp_path):
        """rate_limit_exceeded 冷却 60s。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.mark_failure("a", "rate_limit_exceeded")

        with pool._connect() as conn:
            row = conn.execute("SELECT status, cooldown_until FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "cooling"
        assert row["cooldown_until"] > time.time() + 55  # ~60s

    def test_upstream_unauthorized_cooldown_30s(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.mark_failure("a", "upstream_unauthorized")

        with pool._connect() as conn:
            row = conn.execute("SELECT status, cooldown_until FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "cooling"
        assert row["cooldown_until"] > time.time() + 25

    def test_upstream_5xx_no_cooldown(self, tmp_path):
        """upstream_5xx 不冷却，但记录失败计数。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.mark_failure("a", "upstream_5xx")

        with pool._connect() as conn:
            row = conn.execute("SELECT status, consecutive_failures FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "enabled"
        assert row["consecutive_failures"] == 1

    def test_unknown_code_no_cooldown(self, tmp_path):
        """未知 code 不冷却。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.mark_failure("a", "some_unknown_error")

        with pool._connect() as conn:
            row = conn.execute("SELECT status FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "enabled"

    def test_retry_after_overrides_default(self, tmp_path):
        """retry_after 参数覆盖默认冷却时长。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.mark_failure("a", "rate_limit_exceeded", retry_after=300.0)

        with pool._connect() as conn:
            row = conn.execute("SELECT cooldown_until FROM accounts WHERE label='a'").fetchone()
        assert row["cooldown_until"] > time.time() + 290

    def test_mark_failure_with_retry_after_overrides_default(self, tmp_path):
        """retry_after 优先于 _COOLDOWN_MAP 默认值（image_rate_limited 默认 60s，retry_after=120 覆盖）。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.mark_failure("a", "image_rate_limited", retry_after=120.0)

        with pool._connect() as conn:
            row = conn.execute("SELECT status, cooldown_until FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "cooling"
        # cooldown_until 应接近 now + 120，不是 now + 60
        assert row["cooldown_until"] > time.time() + 100  # ~120s，余量容错

    def test_mark_failure_tiered_minute_hour_day(self, tmp_path):
        """分级 cooldown：minute/hour/day exhausted 各有不同默认时长。"""
        pool = make_pool(tmp_path)

        for code in ["quota_minute_exhausted", "quota_hour_exhausted", "quota_day_exhausted"]:
            pool.upsert_account(acc(code))

        pool.mark_failure("quota_minute_exhausted", "quota_minute_exhausted")
        pool.mark_failure("quota_hour_exhausted", "quota_hour_exhausted")
        pool.mark_failure("quota_day_exhausted", "quota_day_exhausted")

        now = time.time()
        with pool._connect() as conn:
            row_min = conn.execute(
                "SELECT cooldown_until FROM accounts WHERE label='quota_minute_exhausted'"
            ).fetchone()
            row_hour = conn.execute(
                "SELECT cooldown_until FROM accounts WHERE label='quota_hour_exhausted'"
            ).fetchone()
            row_day = conn.execute(
                "SELECT cooldown_until FROM accounts WHERE label='quota_day_exhausted'"
            ).fetchone()

        # minute: ~60s
        assert row_min["cooldown_until"] > now + 55
        assert row_min["cooldown_until"] < now + 90  # 不应超过 1.5 分钟

        # hour: ~3600s
        assert row_hour["cooldown_until"] > now + 3500

        # day: ~86400s
        assert row_day["cooldown_until"] > now + 86000

    def test_5xx_no_cooldown_but_inc_failures(self, tmp_path):
        """upstream_5xx 不触发冷却，但 consecutive_failures 和 fail_count 都增长。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        pool.mark_failure("a", "upstream_5xx")
        pool.mark_failure("a", "upstream_5xx")

        with pool._connect() as conn:
            row = conn.execute(
                "SELECT status, cooldown_until, consecutive_failures, fail_count FROM accounts WHERE label='a'"
            ).fetchone()
        assert row["status"] == "enabled"        # 不冷却
        assert row["cooldown_until"] == 0.0      # 无冷却时间
        assert row["consecutive_failures"] == 2  # 失败计数增长
        assert row["fail_count"] == 2


# ── auto_disabled ─────────────────────────────────────────────────────────────

class TestAutoDisabled:
    def test_consecutive_failures_5_auto_disables(self, tmp_path):
        """连续 5 次失败 → status = auto_disabled，不再被 acquire。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.upsert_account(acc("b"))

        for _ in range(5):
            pool.mark_failure("a", "image_rate_limited")

        with pool._connect() as conn:
            row = conn.execute("SELECT status, consecutive_failures FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "auto_disabled"
        assert row["consecutive_failures"] >= 5

        # acquire 不再选 a
        with pool.acquire() as acq:
            assert acq.label == "b"

    def test_auto_disabled_all_accounts_returns_fallback(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("only"))

        for _ in range(5):
            pool.mark_failure("only", "image_rate_limited")

        with pool.acquire() as acq:
            assert acq.label == "_settings_fallback"


# ── mark_success ──────────────────────────────────────────────────────────────

class TestMarkSuccess:
    def test_success_resets_consecutive_failures(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.mark_failure("a", "upstream_5xx")
        pool.mark_failure("a", "upstream_5xx")
        pool.mark_success("a")

        with pool._connect() as conn:
            row = conn.execute("SELECT consecutive_failures, success_count FROM accounts WHERE label='a'").fetchone()
        assert row["consecutive_failures"] == 0
        assert row["success_count"] == 1

    def test_auto_exit_calls_mark_success(self, tmp_path):
        """with 块正常退出时（未手动 mark）应 mark_success。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.mark_failure("a", "upstream_5xx")  # consecutive_failures = 1

        with pool.acquire() as _acq:
            pass  # 不手动 mark，退出时自动 mark_success

        with pool._connect() as conn:
            row = conn.execute("SELECT consecutive_failures FROM accounts WHERE label='a'").fetchone()
        assert row["consecutive_failures"] == 0


# ── import_from_settings ──────────────────────────────────────────────────────

class TestImportFromSettings:
    def _make_settings(self, cookie: str):
        from mini_grok_api.config import load_settings
        import dataclasses
        s = load_settings()
        return dataclasses.replace(s, grok_cookie=cookie)

    def test_import_creates_default_account(self, tmp_path):
        pool = make_pool(tmp_path)
        settings = self._make_settings("test_cookie_value")
        result = pool.import_from_settings(settings)
        assert result is True

        acct = pool.get_account("default")
        assert acct is not None
        assert acct.cookie == "test_cookie_value"
        assert acct.label == "default"
        assert acct.priority == 1
        assert acct.weight == 10

    def test_import_skips_if_accounts_exist(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("existing"))
        settings = self._make_settings("some_cookie")
        result = pool.import_from_settings(settings)
        assert result is False
        assert pool.get_account("default") is None

    def test_import_skips_if_no_cookie(self, tmp_path):
        pool = make_pool(tmp_path)
        settings = self._make_settings("")
        result = pool.import_from_settings(settings)
        assert result is False
        assert pool.count() == 0

    def test_import_does_not_repeat(self, tmp_path):
        pool = make_pool(tmp_path)
        settings = self._make_settings("cookie_abc")
        r1 = pool.import_from_settings(settings)
        r2 = pool.import_from_settings(settings)
        assert r1 is True
        assert r2 is False
        assert pool.count() == 1


# ── set_enabled ───────────────────────────────────────────────────────────────

class TestSetEnabled:
    def test_disable_account_not_acquired(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.upsert_account(acc("b"))

        changed = pool.set_enabled("a", False)
        assert changed is True

        with pool.acquire() as acq:
            assert acq.label == "b"

    def test_enable_account_clears_state(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        pool.set_enabled("a", False)
        with pool._connect() as conn:
            row = conn.execute("SELECT status, enabled FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "manually_disabled"
        assert row["enabled"] == 0

        pool.set_enabled("a", True)
        with pool._connect() as conn:
            row = conn.execute("SELECT status, enabled, consecutive_failures FROM accounts WHERE label='a'").fetchone()
        assert row["status"] == "enabled"
        assert row["enabled"] == 1
        assert row["consecutive_failures"] == 0

    def test_set_enabled_no_change_returns_false(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        result = pool.set_enabled("a", True)  # 已经是 enabled
        assert result is False

    def test_set_enabled_nonexistent_returns_false(self, tmp_path):
        pool = make_pool(tmp_path)
        result = pool.set_enabled("nonexistent", False)
        assert result is False


# ── upsert / delete / list_accounts ──────────────────────────────────────────

class TestUpsertDelete:
    def test_upsert_new_account(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("x", cookie="cx"))
        acct = pool.get_account("x")
        assert acct is not None
        assert acct.cookie == "cx"

    def test_upsert_updates_credentials_keeps_runtime_state(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("x", cookie="old"))
        pool.mark_failure("x", "upstream_5xx")  # consecutive_failures = 1

        pool.upsert_account(acc("x", cookie="new"))
        acct = pool.get_account("x")
        assert acct.cookie == "new"

        # 运行时状态（consecutive_failures）应保持
        with pool._connect() as conn:
            row = conn.execute("SELECT consecutive_failures FROM accounts WHERE label='x'").fetchone()
        assert row["consecutive_failures"] == 1

    def test_delete_existing(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("x"))
        result = pool.delete_account("x")
        assert result is True
        assert pool.get_account("x") is None

    def test_delete_nonexistent(self, tmp_path):
        pool = make_pool(tmp_path)
        result = pool.delete_account("nope")
        assert result is False

    def test_list_accounts_order(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("z", priority=2))
        pool.upsert_account(acc("a", priority=1))
        pool.upsert_account(acc("m", priority=1))

        infos = pool.list_accounts()
        labels = [i.label for i in infos]
        # priority=1 在前，同 priority 按 label asc
        assert labels == ["a", "m", "z"]

    def test_list_accounts_contains_info_fields(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a", cookie="mycookie_longerthannormal_value_abc"))
        infos = pool.list_accounts()
        assert len(infos) == 1
        info = infos[0]
        assert isinstance(info, AccountInfo)
        assert info.cookie_masked.endswith("...")
        assert not info.cookie_masked.endswith(info.cookie_masked[:-3] + "real_end")  # 已截断

    def test_get_account_none_for_missing(self, tmp_path):
        pool = make_pool(tmp_path)
        assert pool.get_account("ghost") is None


# ── AccountAcquisition 手动 mark ─────────────────────────────────────────────

class TestAcquisitionMark:
    def test_manual_mark_failure(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))
        pool.upsert_account(acc("b"))

        with pool.acquire() as acq:
            acq.mark_failure("image_rate_limited")

        # a 应进入 cooling
        with pool._connect() as conn:
            row = conn.execute("SELECT status FROM accounts WHERE label=?", (acq.label,)).fetchone()
        assert row["status"] == "cooling"

    def test_manual_mark_success_no_double_mark(self, tmp_path):
        """手动 mark_success 后，__exit__ 不应再次 mark（_marked=True 保护）。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("a"))

        with pool.acquire() as acq:
            acq.mark_success()

        with pool._connect() as conn:
            row = conn.execute("SELECT success_count FROM accounts WHERE label='a'").fetchone()
        # 只计了 1 次（mark_success），不是 2 次
        assert row["success_count"] == 1

    def test_fallback_mark_noop(self, tmp_path):
        """_settings_fallback 的 mark 为 no-op，不报错。"""
        pool = make_pool(tmp_path)  # 空 DB

        with pool.acquire() as acq:
            assert acq.label == "_settings_fallback"
            acq.mark_failure("some_code")  # should not raise
            acq.mark_success()             # should not raise


# ── 三账号综合场景 ────────────────────────────────────────────────────────────

class TestMultiAccountScenario:
    def test_three_accounts_round_robin(self, tmp_path):
        """3 个同 priority 账号 LRU 轮换 3 次不重复。"""
        pool = make_pool(tmp_path)
        for label in ["x", "y", "z"]:
            pool.upsert_account(acc(label, priority=1))

        labels = []
        for _ in range(3):
            with pool.acquire() as acq:
                labels.append(acq.label)

        assert set(labels) == {"x", "y", "z"}, f"期望全覆盖，实际: {labels}"

    def test_high_priority_always_picked_first(self, tmp_path):
        """不同 priority，始终选最小 priority 的组。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("hp", priority=1))
        pool.upsert_account(acc("lp", priority=2))

        for _ in range(3):
            with pool.acquire() as acq:
                assert acq.label == "hp"

    def test_fallback_to_lower_priority_when_high_cooling(self, tmp_path):
        """高 priority 全部冷却时，fallback 到低 priority。"""
        pool = make_pool(tmp_path)
        pool.upsert_account(acc("hp", priority=1))
        pool.upsert_account(acc("lp", priority=2))

        pool.mark_failure("hp", "rate_limit_exceeded")

        with pool.acquire() as acq:
            assert acq.label == "lp"
