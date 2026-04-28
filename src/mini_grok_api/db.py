"""SQLite 请求日志存储模块。"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_DIR = Path("./data/file")
DB_PATH = _DATA_DIR / "xgate.db"


class LogDB:
    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE INDEX IF NOT EXISTS idx_chat_ts  ON chat_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_image_ts ON image_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_video_ts ON video_logs(created_at);
            """)

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
        return {
            "chat": {"total": chat["total"], "success": chat["ok"], "error": chat["err"]},
            "images": {
                "total_requests": img["total_reqs"],
                "total_images": img["total_images"],
                "success": img["ok"],
                "error": img["err"],
            },
            "videos": {"total": vid["total"], "success": vid["ok"], "error": vid["err"]},
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
        if log_type in ("all", "chat"):
            tables.append(("chat_logs", "chat"))
        if log_type in ("all", "image"):
            tables.append(("image_logs", "image"))
        if log_type in ("all", "video"):
            tables.append(("video_logs", "video"))

        if not tables:
            return [], 0

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

        if len(tables) > 1:
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
    def _to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        if "image_paths" in d and isinstance(d.get("image_paths"), str):
            try:
                d["image_paths"] = json.loads(d["image_paths"])
            except Exception:
                pass
        return d

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
