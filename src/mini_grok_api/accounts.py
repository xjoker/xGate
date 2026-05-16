"""AccountPool — 多账号管理服务。

全局单例在 main.py lifespan 内初始化，账号凭证和运行时状态均持久化在 SQLite。
调用方获得 AccountAcquisition 上下文后，从 acq.settings 取到已派生的局部 Settings，
直接传给现有 grok_client 函数，无需改动 grok_client.py。
"""

from __future__ import annotations

import dataclasses
import json
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

# ── 冷却时长规则表（Phase 2 分级版）──────────────────────────────────────────
# code → 冷却秒数；None 表示不冷却（仍记录失败）
# 优先级：mark_failure(retry_after=X) > 此表默认值
_COOLDOWN_MAP: dict[str, float | None] = {
    "image_rate_limited":      60.0,
    "rate_limit_exceeded":     60.0,
    "quota_minute_exhausted":  60.0,
    "quota_hour_exhausted":    3600.0,
    "quota_day_exhausted":     86400.0,   # 24h 简化；下个 UTC 0 点细化留 Phase 3
    "upstream_unauthorized":   30.0,
    "cloudflare_challenge":    30.0,
    "upstream_5xx":            None,  # 只计 consecutive_failures，不冷却
    "timeout":                 None,
}

_AUTO_DISABLE_THRESHOLD = 5  # consecutive_failures 达到此值后 auto_disabled


# ── 异常类型 ──────────────────────────────────────────────────────────────────
class AccountPoolError(Exception):
    """AccountPool 基础异常。"""


class UnknownAccountError(AccountPoolError):
    """force_label 指向不存在的账号。"""
    def __init__(self, label: str) -> None:
        super().__init__(f"account label {label!r} not found")
        self.label = label


class AccountDisabledError(AccountPoolError):
    """force_label 指向已禁用的账号。"""
    def __init__(self, label: str, *, status: str) -> None:
        super().__init__(f"account {label!r} is disabled (status={status})")
        self.label = label
        self.status = status


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
                    fail_count           INTEGER NOT NULL DEFAULT 0,
                    quota_cache_json     TEXT    NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_accounts_priority ON accounts(priority);
                CREATE INDEX IF NOT EXISTS idx_accounts_status   ON accounts(status);

                -- 0.3.2: conversation_id → account_label sticky binding
                -- 让 chat 多轮对话固定走同一账号，避免 LRU 切号引发的 Grok 上下文失忆
                CREATE TABLE IF NOT EXISTS conversation_account_map (
                    conversation_id TEXT PRIMARY KEY,
                    account_label   TEXT NOT NULL,
                    created_at      REAL NOT NULL,
                    last_seen       REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conv_acct_last_seen
                    ON conversation_account_map(last_seen);
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

    def _pick_candidates(self) -> list[Account]:
        """返回所有当前可用账号列表（按 priority ASC, last_used_at ASC 排序）。"""
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts"
                " WHERE enabled=1"
                "   AND (status='enabled'"
                "     OR (status='cooling' AND cooldown_until <= ?))"
                " ORDER BY priority ASC, last_used_at ASC",
                (now,),
            ).fetchall()
        return [_row_to_account(r) for r in rows]

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

        若提供 model_id，会跳过该模型配额低于 5% 的账号（soft_cooldown）。
        若所有账号均 soft_cooling，则回退到不过滤（让上游真 429 兜底）。

        ## force_label 严格语义
        客户端通过 `X-Account-Label` header 指定账号 → 严格模式：
        - label 不存在  → raise UnknownAccountError
        - label 存在但 enabled=False / status='manually_disabled' → raise AccountDisabledError
        - 其余情况绕过 soft_cooldown 过滤，直接使用指定账号
          （debug / sticky 场景下用户已经明确意图）
        """
        if force_label:
            account = self._pick_account(force_label=force_label)
            if account is None:
                raise UnknownAccountError(force_label)
            if not account.enabled:
                raise AccountDisabledError(force_label, status="manually_disabled")
            # 注意：cooling / auto_disabled 状态保留 enabled=True，按 debug 意图仍允许使用
            # （上游若真 429 会通过 mark_failure 自然更新冷却）
        elif model_id:
            # soft_cooldown 过滤：跳过配额 < 5% 的账号
            original_candidates = self._pick_candidates()
            filtered = [
                a for a in original_candidates
                if not self._is_quota_low(a.label, model_id)
            ]
            if not filtered and original_candidates:
                logger.warning(
                    "all accounts soft_cooling for model=%s, falling back to unfiltered selection",
                    model_id,
                )
                filtered = original_candidates
            account = filtered[0] if filtered else None
        else:
            account = self._pick_account()

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

    # ── quota cache ───────────────────────────────────────────────────────────

    def update_quota(
        self,
        label: str,
        model_id: str,
        *,
        remaining: int,
        total: int,
        reset_at: float,
    ) -> None:
        """更新某账号某模型的配额缓存（写 DB quota_cache_json 字段）。

        缓存格式：
        {
            "grok-4.20-auto": {"remaining": 25, "total": 25,
                               "reset_at": 1715750000.0, "fetched_at": 1715749700.0},
            ...
        }
        """
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT quota_cache_json FROM accounts WHERE label=?", (label,)
            ).fetchone()
            if row is None:
                return
            try:
                cache: dict = json.loads(row["quota_cache_json"] or "{}")
            except Exception:
                cache = {}
            cache[model_id] = {
                "remaining": remaining,
                "total": total,
                "reset_at": reset_at,
                "fetched_at": now,
            }
            conn.execute(
                "UPDATE accounts SET quota_cache_json=? WHERE label=?",
                (json.dumps(cache, ensure_ascii=False), label),
            )

    def get_quota(self, label: str, model_id: str) -> dict | None:
        """读取某账号某模型的配额缓存，无缓存返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT quota_cache_json FROM accounts WHERE label=?", (label,)
            ).fetchone()
            if row is None:
                return None
            try:
                cache: dict = json.loads(row["quota_cache_json"] or "{}")
            except Exception:
                return None
            return cache.get(model_id)

    # ── image quota（special key）────────────────────────────────────────────
    # 图片配额与 chat 不同：上游 model_name 候选不确定（aurora / grok-2-aurora / ...），
    # 探测命中后值复用全局 hint。缓存键固定为 "__image__"，避免与 chat model_id 冲突。
    _IMAGE_QUOTA_KEY = "__image__"

    def update_image_quota(
        self,
        label: str,
        *,
        model_name: str,
        remaining: int,
        total: int,
        reset_at: float,
    ) -> None:
        """更新某账号的图片配额缓存（写到 quota_cache_json['__image__']）。

        额外存 model_name 字段，记录此账号上次成功的 candidate（debug 用，
        全局 hint 由调用方维护以减少探测成本）。
        """
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT quota_cache_json FROM accounts WHERE label=?", (label,)
            ).fetchone()
            if row is None:
                return
            try:
                cache: dict = json.loads(row["quota_cache_json"] or "{}")
            except Exception:
                cache = {}
            cache[self._IMAGE_QUOTA_KEY] = {
                "model_name": model_name,
                "remaining": remaining,
                "total": total,
                "reset_at": reset_at,
                "fetched_at": now,
            }
            conn.execute(
                "UPDATE accounts SET quota_cache_json=? WHERE label=?",
                (json.dumps(cache, ensure_ascii=False), label),
            )

    def get_image_quota(self, label: str) -> dict | None:
        """读取某账号图片配额缓存，无缓存返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT quota_cache_json FROM accounts WHERE label=?", (label,)
            ).fetchone()
            if row is None:
                return None
            try:
                cache: dict = json.loads(row["quota_cache_json"] or "{}")
            except Exception:
                return None
            return cache.get(self._IMAGE_QUOTA_KEY)

    # ── conversation → account sticky binding (0.3.2) ───────────────────────
    # 让 chat 多轮对话固定走同一账号，避免 LRU 切号导致 Grok 上下文丢失。
    # binding TTL 7 天；后台 cleanup loop 删过期行。
    CONVERSATION_TTL_SECONDS = 7 * 86400

    def get_conversation_binding(self, conversation_id: str) -> str | None:
        """读取 conversation 当前绑定的 account_label；未绑定/过期 → None。"""
        if not conversation_id:
            return None
        cutoff = time.time() - self.CONVERSATION_TTL_SECONDS
        with self._connect() as conn:
            row = conn.execute(
                "SELECT account_label FROM conversation_account_map"
                " WHERE conversation_id=? AND last_seen > ?",
                (conversation_id, cutoff),
            ).fetchone()
        return row["account_label"] if row else None

    def set_conversation_binding(self, conversation_id: str, account_label: str) -> None:
        """upsert binding，touch last_seen。"""
        if not conversation_id or not account_label:
            return
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversation_account_map"
                " (conversation_id, account_label, created_at, last_seen)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(conversation_id) DO UPDATE SET"
                "   account_label=excluded.account_label, last_seen=excluded.last_seen",
                (conversation_id, account_label, now, now),
            )

    def delete_conversation_binding(self, conversation_id: str) -> bool:
        """主动删除指定 binding（BUG-G v0.3.6）— sticky 命中失效账号时调用。

        返回 True 表示实际删除了一行；不存在返回 False。
        """
        if not conversation_id:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM conversation_account_map WHERE conversation_id=?",
                (conversation_id,),
            )
            return cur.rowcount > 0

    def cleanup_old_conversation_bindings(self) -> int:
        """删除超过 TTL 的 binding；返回删除行数。"""
        cutoff = time.time() - self.CONVERSATION_TTL_SECONDS
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM conversation_account_map WHERE last_seen <= ?", (cutoff,)
            )
            return cur.rowcount

    def list_conversation_bindings(self, limit: int = 100) -> list[dict]:
        """admin 调试用：列出最近的 binding。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT conversation_id, account_label, created_at, last_seen"
                " FROM conversation_account_map ORDER BY last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _is_quota_low(self, label: str, model_id: str, threshold: float = 0.05) -> bool:
        """判断某账号某模型配额是否低于阈值（默认 5%）。

        无缓存时返回 False（不过滤，让流量通过，让上游真 429 兜底）。
        total <= 0 时同样不过滤。
        """
        q = self.get_quota(label, model_id)
        if q is None:
            return False
        total = q.get("total", 0)
        if total <= 0:
            return False
        return (q.get("remaining", 0) / total) < threshold

    # ── re_enable（后台 revalidate 专用）────────────────────────────────────

    def re_enable(self, label: str) -> bool:
        """将 auto_disabled 账号恢复为 enabled，清零 consecutive_failures 和 cooldown。

        与 set_enabled(label, True) 的区别：不检查 enabled 列，直接更新 status。
        manually_disabled 账号调用此方法同样有效（不区分来源），调用方应先过滤。
        返回 True 表示实际发生了更新。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM accounts WHERE label=?", (label,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE accounts SET enabled=1, status='enabled',"
                "  consecutive_failures=0, cooldown_until=0"
                " WHERE label=?",
                (label,),
            )
            return True

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

    def list_account_quotas(self) -> list[dict]:
        """返回每个账号的配额缓存摘要，供 admin UI 展示。

        每项格式：{"label": "xxx", "quotas": {"grok-4.20-auto": {...}, ...}}
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT label, quota_cache_json FROM accounts ORDER BY priority ASC, label ASC"
            ).fetchall()
        result = []
        for row in rows:
            try:
                quotas = json.loads(row["quota_cache_json"] or "{}")
            except Exception:
                quotas = {}
            result.append({"label": row["label"], "quotas": quotas})
        return result

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
        """删除账号，返回是否存在（True=删了，False=不存在）。

        SAST round 4 (P3) 补强：同事务清理 `conversation_account_map` 中绑定到
        该 label 的孤儿行，避免 `/admin/conversation-bindings` 列出已删账号的
        历史 binding（数据保留/隐私）。
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE label=?", (label,))
            conn.execute(
                "DELETE FROM conversation_account_map WHERE account_label=?", (label,),
            )
            return cur.rowcount > 0

    # ── import_from_settings ──────────────────────────────────────────────────

    def import_from_settings(self, settings: "Settings", *, force_refresh_default: bool = False) -> bool:
        """启动时 lifespan 调用，或 admin/config 更新凭证后同步 default 账号。

        若 DB 空且 settings.grok_cookie 非空，则 import 为 label='default' 账号。
        防止重复 import（DB 中已有任何行时跳过），除非 force_refresh_default=True。

        force_refresh_default=True：若 label='default' 账号已存在，则 upsert（更新凭证），
        保留 enabled/priority/weight 等配置字段及所有运行时状态（cooldown/counts）。

        返回是否实际执行了 import/update。
        """
        if not settings.grok_cookie:
            return False

        if force_refresh_default:
            # 强制同步 default 账号凭证（admin/config 改 cookie 时调用）
            existing = self.get_account("default")
            if existing is not None:
                # 保留现有的 enabled/priority/weight，只更新凭证字段
                updated = Account(
                    label="default",
                    cookie=settings.grok_cookie,
                    user_agent=settings.grok_user_agent,
                    browser=settings.grok_browser,
                    proxy=settings.grok_proxy,
                    statsig_id=settings.grok_statsig_id,
                    enabled=existing.enabled,
                    priority=existing.priority,
                    weight=existing.weight,
                )
                self.upsert_account(updated)
                logger.info("AccountPool: default account credentials refreshed from settings")
                return True
            # default 不存在但 force_refresh_default=True → 新建
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
            logger.info("AccountPool: imported 'default' account from settings (force_refresh)")
            return True

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
