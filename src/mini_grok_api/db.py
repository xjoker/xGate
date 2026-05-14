"""SQLite 请求日志存储模块。"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 使用 __file__ 锚定项目根目录，避免 systemd/Docker 切换工作目录时读到错误位置的 db。
# 目录结构：src/mini_grok_api/db.py → 向上 3 级 = 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _PROJECT_ROOT / "data" / "file"
DB_PATH = _DATA_DIR / "xgate.db"


class LogDB:
    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path.resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("xgate db path: %s", self._path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chat_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id  TEXT    NOT NULL,
                    created_at  REAL    NOT NULL,
                    model       TEXT    NOT NULL DEFAULT '',
                    prompt      TEXT    NOT NULL DEFAULT '',
                    response    TEXT,
                    status      TEXT    NOT NULL DEFAULT 'success',
                    duration_ms INTEGER,
                    error       TEXT
                );
                CREATE TABLE IF NOT EXISTS image_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id  TEXT    NOT NULL,
                    created_at  REAL    NOT NULL,
                    model       TEXT    NOT NULL DEFAULT '',
                    prompt      TEXT    NOT NULL DEFAULT '',
                    image_paths TEXT,
                    image_count INTEGER NOT NULL DEFAULT 0,
                    aspect_ratio TEXT   NOT NULL DEFAULT '',
                    source      TEXT    NOT NULL DEFAULT 'api',
                    status      TEXT    NOT NULL DEFAULT 'success',
                    duration_ms INTEGER,
                    error       TEXT
                );
                CREATE TABLE IF NOT EXISTS video_logs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id   TEXT    NOT NULL,
                    created_at   REAL    NOT NULL,
                    model        TEXT    NOT NULL DEFAULT '',
                    prompt       TEXT    NOT NULL DEFAULT '',
                    video_path   TEXT,
                    session_id   TEXT    NOT NULL DEFAULT '',
                    aspect_ratio TEXT    NOT NULL DEFAULT '',
                    duration_sec INTEGER NOT NULL DEFAULT 0,
                    resolution   TEXT    NOT NULL DEFAULT '',
                    source       TEXT    NOT NULL DEFAULT 'api',
                    status       TEXT    NOT NULL DEFAULT 'success',
                    duration_ms  INTEGER,
                    error        TEXT
                );
                CREATE TABLE IF NOT EXISTS system_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at  REAL    NOT NULL,
                    event_type  TEXT    NOT NULL DEFAULT '',
                    status      TEXT    NOT NULL DEFAULT 'success',
                    duration_ms INTEGER,
                    detail      TEXT    NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS task_queue (
                    id               TEXT    PRIMARY KEY,
                    kind             TEXT    NOT NULL DEFAULT 'manual',
                    origin           TEXT    NOT NULL DEFAULT 'queue',
                    prompt           TEXT    NOT NULL DEFAULT '',
                    target_count     INTEGER NOT NULL DEFAULT 1,
                    generated_count  INTEGER NOT NULL DEFAULT 0,
                    failed_count     INTEGER NOT NULL DEFAULT 0,
                    attempt_cap      INTEGER NOT NULL DEFAULT 0,
                    aspect_ratio     TEXT    NOT NULL DEFAULT '1:1',
                    enable_pro       INTEGER NOT NULL DEFAULT 0,
                    interval_seconds REAL    NOT NULL DEFAULT 5.0,
                    status           TEXT    NOT NULL DEFAULT 'pending',
                    priority         INTEGER NOT NULL DEFAULT 0,
                    session_id       TEXT    NOT NULL DEFAULT '',
                    created_at       REAL    NOT NULL,
                    started_at       REAL,
                    finished_at      REAL,
                    error            TEXT    NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS file_deletes (
                    id          TEXT    PRIMARY KEY,
                    asset_id    TEXT    NOT NULL DEFAULT '',
                    status      TEXT    NOT NULL DEFAULT 'pending',
                    error       TEXT    NOT NULL DEFAULT '',
                    attempt     INTEGER NOT NULL DEFAULT 0,
                    created_at  REAL    NOT NULL,
                    finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_file_del_st ON file_deletes(status);
                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    csrf       TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS mcp_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id  TEXT    NOT NULL,
                    created_at  REAL    NOT NULL,
                    tool        TEXT    NOT NULL DEFAULT '',
                    model       TEXT    NOT NULL DEFAULT '',
                    prompt      TEXT    NOT NULL DEFAULT '',
                    response    TEXT,
                    status      TEXT    NOT NULL DEFAULT 'success',
                    duration_ms INTEGER,
                    error       TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_mcp_ts ON mcp_logs(created_at);
                CREATE TABLE IF NOT EXISTS grok_assets (
                    asset_id          TEXT    PRIMARY KEY,
                    asset_key         TEXT    NOT NULL DEFAULT '',
                    name              TEXT    NOT NULL DEFAULT '',
                    mime_type         TEXT    NOT NULL DEFAULT '',
                    size_bytes        INTEGER NOT NULL DEFAULT 0,
                    width             INTEGER NOT NULL DEFAULT 0,
                    height            INTEGER NOT NULL DEFAULT 0,
                    create_time       TEXT    NOT NULL DEFAULT '',
                    preview_image_key TEXT    NOT NULL DEFAULT '',
                    metadata_json     TEXT    NOT NULL DEFAULT '',
                    local_path        TEXT    NOT NULL DEFAULT '',
                    downloaded_at     REAL,
                    cloud_deleted_at  REAL,
                    discovered_at     REAL    NOT NULL,
                    last_seen_at      REAL    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_grok_asset_create ON grok_assets(create_time DESC);
                CREATE INDEX IF NOT EXISTS idx_grok_asset_dl    ON grok_assets(downloaded_at);
                CREATE TABLE IF NOT EXISTS file_downloads (
                    id          TEXT    PRIMARY KEY,
                    asset_key   TEXT    NOT NULL DEFAULT '',
                    filename    TEXT    NOT NULL DEFAULT '',
                    size_bytes  INTEGER NOT NULL DEFAULT 0,
                    status      TEXT    NOT NULL DEFAULT 'pending',
                    path        TEXT    NOT NULL DEFAULT '',
                    error       TEXT    NOT NULL DEFAULT '',
                    attempt     INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL,
                    created_at  REAL    NOT NULL,
                    finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_file_dl_ts ON file_downloads(created_at);
                CREATE INDEX IF NOT EXISTS idx_file_dl_st ON file_downloads(status);
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
                CREATE INDEX IF NOT EXISTS idx_chat_ts   ON chat_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_image_ts  ON image_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_video_ts  ON video_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_system_ts ON system_logs(created_at);
            """)
            # 已存在表的列迁移
            existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(grok_assets)").fetchall()}
            if "cloud_deleted_at" not in existing_cols:
                try:
                    conn.execute("ALTER TABLE grok_assets ADD COLUMN cloud_deleted_at REAL")
                except Exception:
                    pass
            if "unavailable_at" not in existing_cols:
                try:
                    conn.execute("ALTER TABLE grok_assets ADD COLUMN unavailable_at REAL")
                    conn.execute("ALTER TABLE grok_assets ADD COLUMN unavailable_reason TEXT NOT NULL DEFAULT ''")
                except Exception:
                    pass
            # 列迁移完成后再建索引
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_asset_del   ON grok_assets(cloud_deleted_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grok_asset_unav ON grok_assets(unavailable_at)")
            # file_downloads 列迁移
            dl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(file_downloads)").fetchall()}
            if "attempt" not in dl_cols:
                try: conn.execute("ALTER TABLE file_downloads ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
                except Exception: pass
            if "next_retry_at" not in dl_cols:
                try: conn.execute("ALTER TABLE file_downloads ADD COLUMN next_retry_at REAL")
                except Exception: pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_dl_retry ON file_downloads(status, next_retry_at)")
            # task_queue 列迁移
            tq_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_queue)").fetchall()}
            if "kind" not in tq_cols:
                try: conn.execute("ALTER TABLE task_queue ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'")
                except Exception: pass
            if "moderated_count" not in tq_cols:
                try: conn.execute("ALTER TABLE task_queue ADD COLUMN moderated_count INTEGER NOT NULL DEFAULT 0")
                except Exception: pass
            if "attempt_cap" not in tq_cols:
                try: conn.execute("ALTER TABLE task_queue ADD COLUMN attempt_cap INTEGER NOT NULL DEFAULT 0")
                except Exception: pass
            if "origin" not in tq_cols:
                try: conn.execute("ALTER TABLE task_queue ADD COLUMN origin TEXT NOT NULL DEFAULT 'queue'")
                except Exception: pass

    # ── Write ─────────────────────────────────────────────────────────────────

    def log_chat(
        self,
        *,
        request_id: str,
        model: str,
        prompt: str,
        response: str | None = None,
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO chat_logs"
                    " (request_id,created_at,model,prompt,response,status,duration_ms,error)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (request_id, time.time(), model, prompt, response, status, duration_ms, error),
                )
        except Exception as exc:
            logger.warning("log_chat failed: %s", exc)

    def log_image(
        self,
        *,
        request_id: str,
        model: str,
        prompt: str,
        image_paths: list[str] | None = None,
        image_count: int = 0,
        aspect_ratio: str = "",
        source: str = "api",
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO image_logs"
                    " (request_id,created_at,model,prompt,image_paths,image_count,"
                    "  aspect_ratio,source,status,duration_ms,error)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request_id, time.time(), model, prompt,
                        json.dumps(image_paths or []), image_count,
                        aspect_ratio, source, status, duration_ms, error,
                    ),
                )
        except Exception as exc:
            logger.warning("log_image failed: %s", exc)

    def log_video(
        self,
        *,
        request_id: str,
        model: str,
        prompt: str,
        video_path: str = "",
        session_id: str = "",
        aspect_ratio: str = "",
        duration_sec: int = 0,
        resolution: str = "",
        source: str = "api",
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO video_logs"
                    " (request_id,created_at,model,prompt,video_path,session_id,"
                    "  aspect_ratio,duration_sec,resolution,source,status,duration_ms,error)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request_id, time.time(), model, prompt, video_path, session_id,
                        aspect_ratio, duration_sec, resolution, source, status, duration_ms, error,
                    ),
                )
        except Exception as exc:
            logger.warning("log_video failed: %s", exc)

    def log_mcp(
        self,
        *,
        request_id: str,
        tool: str,
        model: str = "",
        prompt: str = "",
        response: str | None = None,
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO mcp_logs"
                    " (request_id,created_at,tool,model,prompt,response,status,duration_ms,error)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (request_id, time.time(), tool, model, prompt, response, status, duration_ms, error),
                )
        except Exception as exc:
            logger.warning("log_mcp failed: %s", exc)

    def log_system(
        self,
        *,
        event_type: str,
        status: str = "success",
        duration_ms: int = 0,
        detail: str = "",
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO system_logs (created_at,event_type,status,duration_ms,detail)"
                    " VALUES (?,?,?,?,?)",
                    (time.time(), event_type, status, duration_ms, detail),
                )
        except Exception as exc:
            logger.warning("log_system failed: %s", exc)

    # ── Read ──────────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            chat = dict(conn.execute(
                "SELECT COUNT(*) total,"
                " COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) ok,"
                " COALESCE(SUM(CASE WHEN status='error'   THEN 1 ELSE 0 END),0) err"
                " FROM chat_logs"
            ).fetchone())
            img = dict(conn.execute(
                "SELECT COUNT(*) total_reqs,"
                " COALESCE(SUM(image_count),0) total_images,"
                " COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) ok,"
                " COALESCE(SUM(CASE WHEN status='error'   THEN 1 ELSE 0 END),0) err"
                " FROM image_logs"
            ).fetchone())
            vid = dict(conn.execute(
                "SELECT COUNT(*) total,"
                " COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) ok,"
                " COALESCE(SUM(CASE WHEN status='error'   THEN 1 ELSE 0 END),0) err"
                " FROM video_logs"
            ).fetchone())
            week_ago = time.time() - 7 * 86400
            c7 = dict(conn.execute(
                "SELECT"
                " (SELECT COUNT(*) FROM chat_logs  WHERE created_at>?) chat,"
                " (SELECT COALESCE(SUM(image_count),0) FROM image_logs WHERE created_at>?) imgs,"
                " (SELECT COUNT(*) FROM video_logs WHERE created_at>?) videos",
                (week_ago, week_ago, week_ago),
            ).fetchone())
            # Daily breakdown for last 14 days (for sparkline)
            day14_ago = time.time() - 14 * 86400
            raw_chat = conn.execute(
                "SELECT date(created_at,'unixepoch','localtime') d, COUNT(*) c"
                " FROM chat_logs WHERE created_at>? GROUP BY d ORDER BY d",
                (day14_ago,),
            ).fetchall()
            raw_img = conn.execute(
                "SELECT date(created_at,'unixepoch','localtime') d, COALESCE(SUM(image_count),0) c"
                " FROM image_logs WHERE created_at>? GROUP BY d ORDER BY d",
                (day14_ago,),
            ).fetchall()
            raw_vid = conn.execute(
                "SELECT date(created_at,'unixepoch','localtime') d, COUNT(*) c"
                " FROM video_logs WHERE created_at>? GROUP BY d ORDER BY d",
                (day14_ago,),
            ).fetchall()
            mcp = dict(conn.execute(
                "SELECT COUNT(*) total,"
                " COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) ok,"
                " COALESCE(SUM(CASE WHEN status='error'   THEN 1 ELSE 0 END),0) err"
                " FROM mcp_logs"
            ).fetchone())
        return {
            "chat": {"total": chat["total"], "success": chat["ok"], "error": chat["err"]},
            "images": {
                "total_requests": img["total_reqs"],
                "total_images": img["total_images"],
                "success": img["ok"],
                "error": img["err"],
            },
            "videos": {"total": vid["total"], "success": vid["ok"], "error": vid["err"]},
            "mcp": {"total": mcp["total"], "success": mcp["ok"], "error": mcp["err"]},
            "last_7d": {"chat": c7["chat"], "images": c7["imgs"], "videos": c7["videos"]},
            "daily_chat":   [{"date": r["d"], "count": r["c"]} for r in raw_chat],
            "daily_images": [{"date": r["d"], "count": r["c"]} for r in raw_img],
            "daily_videos": [{"date": r["d"], "count": r["c"]} for r in raw_vid],
        }

    def query(
        self,
        *,
        log_type: str = "all",
        search: str = "",
        offset: int = 0,
        limit: int = 50,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> tuple[list[dict], int]:
        tables: list[tuple[str, str]] = []
        if log_type in ("all", "chat", "dialog"):
            tables.append(("chat_logs", "chat"))
        if log_type in ("all", "image", "media"):
            tables.append(("image_logs", "image"))
        if log_type in ("all", "video", "media"):
            tables.append(("video_logs", "video"))
        if log_type in ("all", "dialog"):
            tables.append(("mcp_logs", "mcp"))

        all_rows: list[dict] = []
        total = 0
        cond, params = self._where(search, from_ts, to_ts)

        for tbl, typ in tables:
            with self._connect() as conn:
                cnt = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE {cond}", params
                ).fetchone()[0]
                total += cnt
                fetch_limit = offset + limit if len(tables) > 1 else limit
                fetch_offset = 0 if len(tables) > 1 else offset
                rows = conn.execute(
                    f"SELECT *,'{typ}' log_type FROM {tbl} WHERE {cond}"
                    f" ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    params + [fetch_limit, fetch_offset],
                ).fetchall()
                all_rows.extend(self._to_dict(r) for r in rows)

        if log_type in ("all", "system"):
            sys_cond, sys_params = self._where_system(search, from_ts, to_ts)
            with self._connect() as conn:
                cnt = conn.execute(
                    f"SELECT COUNT(*) FROM system_logs WHERE {sys_cond}", sys_params
                ).fetchone()[0]
                total += cnt
                fetch_limit = offset + limit if (tables or log_type == "all") else limit
                fetch_offset = 0 if (tables or log_type == "all") else offset
                rows = conn.execute(
                    f"SELECT *,'system' log_type FROM system_logs WHERE {sys_cond}"
                    f" ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    sys_params + [fetch_limit, fetch_offset],
                ).fetchall()
                all_rows.extend(self._to_dict(r) for r in rows)

        if not all_rows:
            return [], total

        if len(tables) > 1:
            # 多 table 合并时需要在内存中重新排序并分页；
            # 单 table 或纯 system 查询时 SQL 已经完成了 OFFSET/LIMIT，不能再切片。
            all_rows.sort(key=lambda x: x["created_at"], reverse=True)
            all_rows = all_rows[offset: offset + limit]

        return all_rows, total

    @staticmethod
    def _where(search: str, from_ts: float | None, to_ts: float | None) -> tuple[str, list]:
        parts = ["1=1"]
        params: list = []
        if search:
            parts.append("(prompt LIKE ? OR model LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if from_ts:
            parts.append("created_at >= ?")
            params.append(from_ts)
        if to_ts:
            parts.append("created_at <= ?")
            params.append(to_ts)
        return " AND ".join(parts), params

    @staticmethod
    def _where_system(search: str, from_ts: float | None, to_ts: float | None) -> tuple[str, list]:
        parts = ["1=1"]
        params: list = []
        if search:
            parts.append("(detail LIKE ? OR event_type LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if from_ts:
            parts.append("created_at >= ?")
            params.append(from_ts)
        if to_ts:
            parts.append("created_at <= ?")
            params.append(to_ts)
        return " AND ".join(parts), params

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        if "image_paths" in d and isinstance(d.get("image_paths"), str):
            try:
                d["image_paths"] = json.loads(d["image_paths"])
            except Exception:
                pass
        return d

    # ── Grok Assets Cache ─────────────────────────────────────────────────────

    def upsert_grok_assets(self, assets: list[dict[str, Any]]) -> tuple[int, int, int]:
        """批量插入/更新 asset 元数据。返回 (新增数, 更新数, 误删纠正数)。

        云端 listing 是 ground truth：本轮出现的 asset 一律清掉 cloud_deleted_at —
        既然 Grok 又把它列出来，说明它就是没被删（或之前误标）。
        """
        new_count = 0
        upd_count = 0
        revived_count = 0  # cloud_deleted_at 被清空的（误删纠正）
        now = time.time()
        try:
            with self._connect() as conn:
                for a in assets:
                    aid = a.get("assetId") or ""
                    if not aid:
                        continue
                    existing = conn.execute(
                        "SELECT cloud_deleted_at FROM grok_assets WHERE asset_id=?", (aid,)
                    ).fetchone()
                    if existing:
                        if existing["cloud_deleted_at"] is not None:
                            revived_count += 1
                        conn.execute(
                            "UPDATE grok_assets SET asset_key=?,name=?,mime_type=?,size_bytes=?,"
                            "width=?,height=?,create_time=?,preview_image_key=?,metadata_json=?,last_seen_at=?,"
                            "cloud_deleted_at=NULL"
                            " WHERE asset_id=?",
                            (
                                a.get("key", ""), a.get("name", ""), a.get("mimeType", ""),
                                int(a.get("sizeBytes") or 0), int(a.get("width") or 0), int(a.get("height") or 0),
                                a.get("createTime", ""), a.get("previewImageKey", ""),
                                json.dumps(a, ensure_ascii=False), now, aid,
                            ),
                        )
                        upd_count += 1
                    else:
                        conn.execute(
                            "INSERT INTO grok_assets"
                            " (asset_id,asset_key,name,mime_type,size_bytes,width,height,"
                            "  create_time,preview_image_key,metadata_json,discovered_at,last_seen_at)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                aid, a.get("key", ""), a.get("name", ""), a.get("mimeType", ""),
                                int(a.get("sizeBytes") or 0), int(a.get("width") or 0), int(a.get("height") or 0),
                                a.get("createTime", ""), a.get("previewImageKey", ""),
                                json.dumps(a, ensure_ascii=False), now, now,
                            ),
                        )
                        new_count += 1
        except Exception as exc:
            logger.warning("upsert_grok_assets failed: %s", exc)
        return new_count, upd_count, revived_count

    def list_grok_assets_db(self, *, only_undownloaded: bool = False, limit: int = 50, offset: int = 0,
                              exclude_dead: bool = True) -> list[dict[str, Any]]:
        """exclude_dead=True 时，过滤掉「云端已删 + 本地未下载」的幽灵记录（默认开）。"""
        try:
            with self._connect() as conn:
                if only_undownloaded:
                    cond = "WHERE downloaded_at IS NULL AND unavailable_at IS NULL AND cloud_deleted_at IS NULL"
                elif exclude_dead:
                    cond = "WHERE NOT (cloud_deleted_at IS NOT NULL AND downloaded_at IS NULL)"
                else:
                    cond = ""
                rows = conn.execute(
                    f"SELECT * FROM grok_assets {cond}"
                    f" ORDER BY create_time DESC, discovered_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                return [self._asset_row_to_dict(r) for r in rows]
        except Exception as exc:
            logger.warning("list_grok_assets_db failed: %s", exc)
            return []

    def count_grok_assets(self, *, only_downloaded: bool = False, only_undownloaded: bool = False,
                           exclude_dead: bool = True) -> int:
        try:
            with self._connect() as conn:
                if only_downloaded:
                    cond = "WHERE downloaded_at IS NOT NULL"
                elif only_undownloaded:
                    cond = "WHERE downloaded_at IS NULL AND unavailable_at IS NULL AND cloud_deleted_at IS NULL"
                elif exclude_dead:
                    cond = "WHERE NOT (cloud_deleted_at IS NOT NULL AND downloaded_at IS NULL)"
                else:
                    cond = ""
                return conn.execute(f"SELECT COUNT(*) FROM grok_assets {cond}").fetchone()[0]
        except Exception:
            return 0

    def mark_asset_downloaded(self, asset_id: str, local_path: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE grok_assets SET local_path=?, downloaded_at=? WHERE asset_id=?",
                    (local_path, time.time(), asset_id),
                )
        except Exception as exc:
            logger.warning("mark_asset_downloaded failed: %s", exc)

    def mark_asset_unavailable(self, asset_id: str, reason: str = "") -> None:
        """标记永久不可用 asset（空 body / 404 / 410），后续不再入下载队列。"""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE grok_assets SET unavailable_at=?, unavailable_reason=? WHERE asset_id=?",
                    (time.time(), reason[:200], asset_id),
                )
        except Exception as exc:
            logger.warning("mark_asset_unavailable failed: %s", exc)

    def mark_asset_cloud_deleted(self, asset_id: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE grok_assets SET cloud_deleted_at=? WHERE asset_id=?",
                    (time.time(), asset_id),
                )
        except Exception as exc:
            logger.warning("mark_asset_cloud_deleted failed: %s", exc)

    def get_downloaded_asset_ids(self) -> set[str]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT asset_id FROM grok_assets WHERE downloaded_at IS NOT NULL"
                ).fetchall()
                return {r[0] for r in rows}
        except Exception:
            return set()

    def get_unavailable_asset_ids(self) -> set[str]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT asset_id FROM grok_assets WHERE unavailable_at IS NOT NULL"
                ).fetchall()
                return {r[0] for r in rows}
        except Exception:
            return set()

    @staticmethod
    def _asset_row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        meta_str = d.pop("metadata_json", "") or ""
        # 还原成 Grok API 兼容的形状（前端 _renderFileCard 需要这些字段）
        try:
            meta = json.loads(meta_str) if meta_str else {}
        except Exception:
            meta = {}
        # 优先用 metadata_json 里的原始字段，缺失则用 DB 列回填
        meta.setdefault("assetId", d["asset_id"])
        meta.setdefault("key", d["asset_key"])
        meta.setdefault("name", d["name"])
        meta.setdefault("mimeType", d["mime_type"])
        meta.setdefault("sizeBytes", d["size_bytes"])
        meta.setdefault("width", d["width"])
        meta.setdefault("height", d["height"])
        meta.setdefault("createTime", d["create_time"])
        meta.setdefault("previewImageKey", d["preview_image_key"])
        meta["_local_downloaded"] = bool(d.get("downloaded_at"))
        meta["_local_path"] = d.get("local_path", "")
        meta["_unavailable"] = bool(d.get("unavailable_at"))
        meta["_unavailable_reason"] = d.get("unavailable_reason", "")
        meta["_cloud_deleted"] = bool(d.get("cloud_deleted_at"))
        return meta

    # ── File Download Queue ───────────────────────────────────────────────────

    def add_file_downloads(self, jobs: list[dict[str, Any]]) -> None:
        try:
            with self._connect() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO file_downloads"
                    " (id,asset_key,filename,size_bytes,status,created_at)"
                    " VALUES (:id,:asset_key,:filename,:size_bytes,'pending',:created_at)",
                    jobs,
                )
        except Exception as exc:
            logger.warning("add_file_downloads failed: %s", exc)

    def claim_pending_downloads(self, limit: int = 4) -> list[dict[str, Any]]:
        """原子领取 pending 任务（含到期 retrying），改为 running 返回。"""
        now = time.time()
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM file_downloads"
                    " WHERE status='pending'"
                    "    OR (status='retrying' AND (next_retry_at IS NULL OR next_retry_at<=?))"
                    " ORDER BY created_at ASC LIMIT ?", (now, limit)
                ).fetchall()
                if not rows:
                    return []
                ids = [r["id"] for r in rows]
                conn.execute(
                    f"UPDATE file_downloads SET status='running'"
                    f" WHERE id IN ({','.join('?'*len(ids))})", ids
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("claim_pending_downloads failed: %s", exc)
            return []

    def finish_download(
        self, job_id: str, *, path: str = "", error: str = "", permanent: bool = False,
        max_attempts: int = 5, base_backoff_seconds: float = 30.0,
    ) -> None:
        """成功 → done；失败 → 若未达 max_attempts 转 retrying（指数退避），否则 failed。
        permanent=True 时跳过重试，直接 failed（用于已知永久错误如空 body / 404）。"""
        try:
            with self._connect() as conn:
                if not error:
                    conn.execute(
                        "UPDATE file_downloads SET status='done',path=?,error='',finished_at=?"
                        " WHERE id=?", (path, time.time(), job_id),
                    )
                    return
                row = conn.execute(
                    "SELECT attempt FROM file_downloads WHERE id=?", (job_id,)
                ).fetchone()
                attempt = (row["attempt"] if row else 0) + 1
                if permanent or attempt >= max_attempts:
                    conn.execute(
                        "UPDATE file_downloads SET status='failed',error=?,attempt=?,finished_at=?"
                        " WHERE id=?", (error, attempt, time.time(), job_id),
                    )
                else:
                    # 指数退避：30s, 60s, 120s, 240s …
                    delay = base_backoff_seconds * (2 ** (attempt - 1))
                    conn.execute(
                        "UPDATE file_downloads SET status='retrying',error=?,attempt=?,next_retry_at=?"
                        " WHERE id=?", (error, attempt, time.time() + delay, job_id),
                    )
        except Exception as exc:
            logger.warning("finish_download failed: %s", exc)

    def list_file_downloads(self, *, since_ts: float = 0.0) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM file_downloads WHERE created_at>=?"
                    " ORDER BY created_at DESC LIMIT 500", (since_ts,)
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("list_file_downloads failed: %s", exc)
            return []

    # ── File Delete Queue ─────────────────────────────────────────────────────

    def add_file_deletes(self, asset_ids: list[str]) -> int:
        if not asset_ids:
            return 0
        try:
            with self._connect() as conn:
                rows = [
                    {"id": str(__import__("uuid").uuid4()), "aid": aid, "ts": time.time()}
                    for aid in asset_ids if aid
                ]
                conn.executemany(
                    "INSERT INTO file_deletes (id,asset_id,status,created_at)"
                    " VALUES (:id,:aid,'pending',:ts)", rows,
                )
                return len(rows)
        except Exception as exc:
            logger.warning("add_file_deletes failed: %s", exc)
            return 0

    def claim_pending_deletes(self, limit: int = 2) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM file_deletes WHERE status='pending'"
                    " ORDER BY created_at ASC LIMIT ?", (limit,)
                ).fetchall()
                if not rows:
                    return []
                ids = [r["id"] for r in rows]
                conn.execute(
                    f"UPDATE file_deletes SET status='running'"
                    f" WHERE id IN ({','.join('?'*len(ids))})", ids
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("claim_pending_deletes failed: %s", exc)
            return []

    def finish_delete(self, job_id: str, *, error: str = "") -> None:
        status = "done" if not error else "failed"
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE file_deletes SET status=?,error=?,finished_at=? WHERE id=?",
                    (status, error, time.time(), job_id),
                )
        except Exception as exc:
            logger.warning("finish_delete failed: %s", exc)

    def reset_running_deletes(self) -> None:
        """服务重启时把 running 的删除任务回滚为 pending（worker 会继续接管）。"""
        try:
            with self._connect() as conn:
                conn.execute("UPDATE file_deletes SET status='pending' WHERE status='running'")
        except Exception as exc:
            logger.warning("reset_running_deletes failed: %s", exc)

    def reset_running_downloads(self) -> None:
        """服务重启时把 running 任务回滚为 pending（保留 attempt/error 不动）。"""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE file_downloads SET status='pending' WHERE status='running'"
                )
        except Exception as exc:
            logger.warning("reset_running_downloads failed: %s", exc)

    def get_dl_queue_overview(self, *, since_ts: float = 0.0) -> dict[str, int]:
        """各状态计数（含 retrying）。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT status, COUNT(*) c FROM file_downloads"
                    " WHERE created_at>=? GROUP BY status", (since_ts,),
                ).fetchall()
                return {r["status"]: r["c"] for r in rows}
        except Exception:
            return {}

    # ── Task Queue Persistence ────────────────────────────────────────────────

    def save_task(self, task: dict[str, Any]) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO task_queue"
                    " (id,kind,origin,prompt,target_count,generated_count,failed_count,moderated_count,attempt_cap,"
                    "  aspect_ratio,enable_pro,interval_seconds,status,priority,"
                    "  session_id,created_at,started_at,finished_at,error)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task["id"], task.get("kind", "manual"), task.get("origin", "queue"),
                        task["prompt"], task["target_count"],
                        task["generated_count"], task["failed_count"], task.get("moderated_count", 0),
                        task.get("attempt_cap", 0),
                        task["aspect_ratio"], int(task["enable_pro"]),
                        task["interval_seconds"], task["status"], task["priority"],
                        task["session_id"], task["created_at"],
                        task.get("started_at"), task.get("finished_at"),
                        task.get("error", ""),
                    ),
                )
        except Exception as exc:
            logger.warning("save_task failed: %s", exc)

    def load_tasks(self) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM task_queue ORDER BY priority ASC, created_at ASC"
                ).fetchall()
                result = []
                for r in rows:
                    d = dict(r)
                    d["enable_pro"] = bool(d["enable_pro"])
                    result.append(d)
                return result
        except Exception as exc:
            logger.warning("load_tasks failed: %s", exc)
            return []

    def delete_task(self, task_id: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM task_queue WHERE id=?", (task_id,))
        except Exception as exc:
            logger.warning("delete_task failed: %s", exc)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        with self._connect() as conn:
            r1 = conn.execute("DELETE FROM chat_logs  WHERE created_at < ?", (cutoff,))
            r2 = conn.execute("DELETE FROM image_logs WHERE created_at < ?", (cutoff,))
            r3 = conn.execute("DELETE FROM video_logs WHERE created_at < ?", (cutoff,))
            n = r1.rowcount + r2.rowcount + r3.rowcount
        if n:
            logger.info("log cleanup: removed %d records (>%d days)", n, retention_days)
        return n


# 全局单例，供 main.py 和 mcp_server.py 共享同一实例
log_db = LogDB()
