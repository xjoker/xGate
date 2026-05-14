"""AccountPool — 多账号管理服务。

全局单例在 main.py lifespan 内初始化，账号凭证和运行时状态均持久化在 SQLite。
调用方获得 AccountAcquisition 上下文后，从 acq.settings 取到已派生的局部 Settings，
直接传给现有 grok_client 函数，无需改动 grok_client.py。
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mini_grok_api.db import DB_PATH

if TYPE_CHECKING:
    from mini_grok_api.config import Settings

logger = logging.getLogger(__name__)

# ── 冷却时长规则表（Phase 1 简化版）──────────────────────────────────────────
# code → 冷却秒数；None 表示不冷却（仍记录失败）
_COOLDOWN_MAP: dict[str, float | None] = {
    "image_rate_limited":    60.0,
    "rate_limit_exceeded":   60.0,
    "upstream_unauthorized": 30.0,
    "cloudflare_challenge":  30.0,
    "upstream_5xx":          None,  # 只计 consecutive_failures，不冷却
    "timeout":               None,
}

_AUTO_DISABLE_THRESHOLD = 5  # consecutive_failures 达到此值后 auto_disabled


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Account:
    label: str
    cookie: str
    user_agent: str
    browser: str        # e.g. "chrome142"
    proxy: str          # 空字符串=不用代理
    statsig_id: str     # 空字符串=用默认
    enabled: bool
    priority: int       # 数字越小越优先
    weight: int         # 同优先级内加权随机（Phase 2 实现，Phase 1 LRU 即可）


@dataclass(slots=True)
class AccountInfo:
    """用于 UI 展示，含运行时状态。"""
    label: str
    enabled: bool
    priority: int
    weight: int
    status: str              # "enabled" | "cooling" | "manually_disabled" | "auto_disabled"
    cooldown_until: float    # unix ts；过期则 = 0
    last_used_at: float
    last_error_code: str
    last_error_at: float
    consecutive_failures: int
    success_count: int
    fail_count: int
    cookie_masked: str       # cookie 前 24 字符 + "..."


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _mask_cookie(cookie: str) -> str:
    if not cookie:
        return ""
    if len(cookie) <= 24:
        return cookie[:4] + "..."
    return cookie[:24] + "..."


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        label=row["label"],
        cookie=row["cookie"],
        user_agent=row["user_agent"],
        browser=row["browser"],
        proxy=row["proxy"],
        statsig_id=row["statsig_id"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
        weight=row["weight"],
    )


def _row_to_info(row: sqlite3.Row) -> AccountInfo:
    return AccountInfo(
        label=row["label"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
        weight=row["weight"],
        status=row["status"],
        cooldown_until=row["cooldown_until"],
        last_used_at=row["last_used_at"],
        last_error_code=row["last_error_code"],
        last_error_at=row["last_error_at"],
        consecutive_failures=row["consecutive_failures"],
        success_count=row["success_count"],
        fail_count=row["fail_count"],
        cookie_masked=_mask_cookie(row["cookie"]),
    )


# ── AccountAcquisition ────────────────────────────────────────────────────────

class AccountAcquisition:
    """contextmanager。进入时已选好 account，退出时按需 mark。"""

    def __init__(self, pool: "AccountPool", account: Account, settings: "Settings") -> None:
        self._pool = pool
        self._account = account
        self._settings = settings
        self._marked = False  # 是否已手动 mark

    @property
    def label(self) -> str:
        return self._account.label

    @property
    def account(self) -> Account:
        return self._account

    @property
    def settings(self) -> "Settings":
        return self._settings

    def __enter__(self) -> "AccountAcquisition":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._marked:
            # 未显式 mark → 按 mark_success 处理
            self._pool.mark_success(self._account.label)

    def mark_failure(self, code: str, *, retry_after: float | None = None) -> None:
        self._marked = True
        self._pool.mark_failure(self._account.label, code, retry_after=retry_after)

    def mark_success(self) -> None:
        self._marked = True
        self._pool.mark_success(self._account.label)


# ── _FallbackAcquisition（_settings_fallback 特殊处理）─────────────────────

class _FallbackAcquisition(AccountAcquisition):
    """settings fallback 账号的 acquisition，不写 DB，mark 操作均为 no-op。"""

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass  # no-op，不写 DB

    def mark_failure(self, code: str, *, retry_after: float | None = None) -> None:
        pass  # no-op

    def mark_success(self) -> None:
        pass  # no-op


# ── AccountPool ───────────────────────────────────────────────────────────────

class AccountPool:
    """全局单例，main.py lifespan 启动时初始化。

    凭证和运行时状态均存 SQLite；进程重启后 cooldown 等状态不丢。
    选号策略：priority 最小优先 + LRU（last_used_at 升序）。
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path.resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_table()

    # ── 内部 DB 辅助 ──────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_table(self) -> None:
        """确保 accounts 表存在（db.py 的 _init 已创建；此处为独立 DB 路径兜底）。"""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    label                TEXT    PRIMARY KEY,
                    cookie               TEXT    NOT NULL DEFAULT '',
                    user_agent           TEXT    NOT NULL DEFAULT '',
                    browser              TEXT    NOT NULL DEFAULT 'chrome142',
                    proxy                TEXT    NOT NULL DEFAULT '',
                    statsig_id           TEXT    NOT NULL DEFAULT '',
                    enabled              INTEGER NOT NULL DEFAULT 1,
                    priority             INTEGER NOT NULL DEFAULT 1,
                    weight               INTEGER NOT NULL DEFAULT 10,
                    status               TEXT    NOT NULL DEFAULT 'enabled',
                    cooldown_until       REAL    NOT NULL DEFAULT 0,
                    last_used_at         REAL    NOT NULL DEFAULT 0,
                    last_error_code      TEXT    NOT NULL DEFAULT '',
                    last_error_at        REAL    NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    success_count        INTEGER NOT NULL DEFAULT 0,
                    fail_count           INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_accounts_priority ON accounts(priority);
                CREATE INDEX IF NOT EXISTS idx_accounts_status   ON accounts(status);
            """)

    # ── 选号逻辑 ──────────────────────────────────────────────────────────────

    def _pick_account(self, *, force_label: str | None = None) -> Account | None:
        """从 DB 选取最优账号，返回 Account 或 None。"""
        now = time.time()
        with self._connect() as conn:
            if force_label:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE label=?", (force_label,)
                ).fetchone()
                if row:
                    return _row_to_account(row)
                return None

            # 可用账号：enabled=True 且 (status='enabled' 或 status='cooling' 但 cooldown 已过)
            rows = conn.execute(
                "SELECT * FROM accounts"
                " WHERE enabled=1"
                "   AND (status='enabled'"
                "     OR (status='cooling' AND cooldown_until <= ?))"
                " ORDER BY priority ASC, last_used_at ASC",
                (now,),
            ).fetchall()

            if not rows:
                return None

            # 取最高优先级（priority 最小）的组
            best_priority = rows[0]["priority"]
            candidates = [r for r in rows if r["priority"] == best_priority]

            # LRU：已是按 last_used_at ASC 排序，取第一个
            return _row_to_account(candidates[0])

    # ── acquire ───────────────────────────────────────────────────────────────

    def acquire(
        self,
        *,
        model_id: str | None = None,
        force_label: str | None = None,
        base_settings: "Settings | None" = None,
    ) -> AccountAcquisition:
        """选号并返回 AccountAcquisition 上下文管理器。

        base_settings 为 None 时，settings 字段使用 Account 本身的凭证构造占位 Settings。
        主调方（main.py 等）应传入 settings_store.get() 作为 base。
        """
        account = self._pick_account(force_label=force_label)

        if account is None:
            # 0-账号兜底：返回 _settings_fallback（不写 DB）
            fallback = Account(
                label="_settings_fallback",
                cookie="",
                user_agent="",
                browser="chrome142",
                proxy="",
                statsig_id="",
                enabled=True,
                priority=999,
                weight=1,
            )
            # 派生 settings（base_settings 为 None 时直接用 fallback 凭证）
            derived = _as_settings(base_settings, fallback) if base_settings else _minimal_settings(fallback)
            return _FallbackAcquisition(self, fallback, derived)

        # 更新 last_used_at（在 pick 之后立即写，避免并发选到同一个；cooldown 保护会兜底）
        now = time.time()
        with self._connect() as conn:
            # 如果 cooling 但已过期，顺手复位 status
            conn.execute(
                "UPDATE accounts SET last_used_at=?,"
                "  status=CASE WHEN status='cooling' AND cooldown_until<=? THEN 'enabled' ELSE status END"
                " WHERE label=?",
                (now, now, account.label),
            )

        derived = _as_settings(base_settings, account) if base_settings else _minimal_settings(account)
        return AccountAcquisition(self, account, derived)

    # ── mark_failure / mark_success ───────────────────────────────────────────

    def mark_failure(self, label: str, code: str, *, retry_after: float | None = None) -> None:
        """根据 code 决定冷却时长并更新状态。"""
        now = time.time()
        cooldown_secs = _COOLDOWN_MAP.get(code, None)  # 未知 code → 不冷却

        with self._connect() as conn:
            row = conn.execute(
                "SELECT consecutive_failures, fail_count FROM accounts WHERE label=?", (label,)
            ).fetchone()
            if row is None:
                return

            new_consecutive = row["consecutive_failures"] + 1
            new_fail = row["fail_count"] + 1

            if new_consecutive >= _AUTO_DISABLE_THRESHOLD:
                new_status = "auto_disabled"
                new_cooldown = 0.0
            elif cooldown_secs is not None:
                new_status = "cooling"
                duration = retry_after if retry_after is not None else cooldown_secs
                new_cooldown = now + duration
            else:
                # 不冷却，但记录失败
                new_status = None  # 保持现有 status（除非即将 auto_disable）
                new_cooldown = None

            if new_status is not None and new_cooldown is not None:
                conn.execute(
                    "UPDATE accounts SET"
                    "  status=?, cooldown_until=?,"
                    "  last_error_code=?, last_error_at=?,"
                    "  consecutive_failures=?, fail_count=?"
                    " WHERE label=?",
                    (new_status, new_cooldown, code, now, new_consecutive, new_fail, label),
                )
            elif new_status is not None:
                # auto_disabled，cooldown 清零
                conn.execute(
                    "UPDATE accounts SET"
                    "  status=?, cooldown_until=0,"
                    "  last_error_code=?, last_error_at=?,"
                    "  consecutive_failures=?, fail_count=?"
                    " WHERE label=?",
                    (new_status, code, now, new_consecutive, new_fail, label),
                )
            else:
                # no cooldown，只更新计数和错误信息
                conn.execute(
                    "UPDATE accounts SET"
                    "  last_error_code=?, last_error_at=?,"
                    "  consecutive_failures=?, fail_count=?"
                    " WHERE label=?",
                    (code, now, new_consecutive, new_fail, label),
                )

    def mark_success(self, label: str) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET"
                "  consecutive_failures=0, success_count=success_count+1,"
                "  last_used_at=?"
                " WHERE label=?",
                (now, label),
            )

    # ── list / get ────────────────────────────────────────────────────────────

    def list_accounts(self) -> list[AccountInfo]:
        """UI 用。按 priority asc, label asc 排序。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY priority ASC, label ASC"
            ).fetchall()
        return [_row_to_info(r) for r in rows]

    def get_account(self, label: str) -> Account | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE label=?", (label,)
            ).fetchone()
        return _row_to_account(row) if row else None

    # ── set_enabled ───────────────────────────────────────────────────────────

    def set_enabled(self, label: str, enabled: bool) -> bool:
        """UI 用。返回是否实际发生变更。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT enabled, status FROM accounts WHERE label=?", (label,)
            ).fetchone()
            if row is None:
                return False

            current_enabled = bool(row["enabled"])
            if current_enabled == enabled:
                return False

            if enabled:
                new_status = "enabled"
                conn.execute(
                    "UPDATE accounts SET enabled=1, status=?, consecutive_failures=0,"
                    "  cooldown_until=0 WHERE label=?",
                    (new_status, label),
                )
            else:
                conn.execute(
                    "UPDATE accounts SET enabled=0, status='manually_disabled' WHERE label=?",
                    (label,),
                )
            return True

    # ── upsert / delete ───────────────────────────────────────────────────────

    def upsert_account(self, account: Account) -> None:
        """新增或更新账号凭证配置（仅更新凭证和配置字段，不重置运行时状态）。"""
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM accounts WHERE label=?", (account.label,)
            ).fetchone()

            if exists:
                # 更新凭证和配置；运行时状态字段（status/cooldown/counts）保持不变
                conn.execute(
                    "UPDATE accounts SET"
                    "  cookie=?, user_agent=?, browser=?, proxy=?, statsig_id=?,"
                    "  enabled=?, priority=?, weight=?"
                    " WHERE label=?",
                    (
                        account.cookie, account.user_agent, account.browser,
                        account.proxy, account.statsig_id,
                        int(account.enabled), account.priority, account.weight,
                        account.label,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO accounts"
                    " (label,cookie,user_agent,browser,proxy,statsig_id,"
                    "  enabled,priority,weight,status)"
                    " VALUES (?,?,?,?,?,?,?,?,?,'enabled')",
                    (
                        account.label, account.cookie, account.user_agent, account.browser,
                        account.proxy, account.statsig_id,
                        int(account.enabled), account.priority, account.weight,
                    ),
                )

    def delete_account(self, label: str) -> bool:
        """删除账号，返回是否存在（True=删了，False=不存在）。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE label=?", (label,))
            return cur.rowcount > 0

    # ── import_from_settings ──────────────────────────────────────────────────

    def import_from_settings(self, settings: "Settings") -> bool:
        """启动时 lifespan 调用。

        若 DB 空且 settings.grok_cookie 非空，则 import 为 label='default' 账号。
        防止重复 import（DB 中已有任何行时跳过）。
        返回是否实际执行了 import。
        """
        if not settings.grok_cookie:
            return False

        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            if count > 0:
                return False  # 已有账号，跳过

        account = Account(
            label="default",
            cookie=settings.grok_cookie,
            user_agent=settings.grok_user_agent,
            browser=settings.grok_browser,
            proxy=settings.grok_proxy,
            statsig_id=settings.grok_statsig_id,
            enabled=True,
            priority=1,
            weight=10,
        )
        self.upsert_account(account)
        logger.info("AccountPool: imported 'default' account from settings")
        return True

    # ── 账号数量 ──────────────────────────────────────────────────────────────

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


# ── Settings 派生辅助 ─────────────────────────────────────────────────────────

def _as_settings(base: "Settings", account: Account) -> "Settings":
    """用 Account 凭证覆盖 base Settings 的五个凭证字段，返回新 Settings 实例。"""
    return dataclasses.replace(
        base,
        grok_cookie=account.cookie,
        grok_user_agent=account.user_agent,
        grok_browser=account.browser,
        grok_proxy=account.proxy,
        grok_statsig_id=account.statsig_id,
    )


def _minimal_settings(account: Account) -> "Settings":
    """在没有 base_settings 的情况下（测试/smoke 用），从 account 构造占位 Settings。"""
    from mini_grok_api.config import load_settings
    base = load_settings()
    return _as_settings(base, account)


# ── 全局单例 ──────────────────────────────────────────────────────────────────
# 模块级实例化；main.py lifespan 会调 import_from_settings() 完成兜底导入。
account_pool: AccountPool = AccountPool()
