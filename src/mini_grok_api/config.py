"""配置加载：data/config/mini.toml 为唯一数据源（不读环境变量）。"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

# 配置路径基于工作目录（cwd），确保 Docker（WORKDIR=/app + 挂载 ./data:/app/data）
# 与本地（项目根目录运行）行为一致。**不能**用 __file__.parents 因为 pip install 后
# 包会到 site-packages，与 ./data 的相对位置完全错位。
ROOT_DIR = Path.cwd()
CONFIG_PATH = ROOT_DIR / "data" / "config" / "mini.toml"

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class Settings:
    server_host: str
    server_port: int
    api_key: str
    grok_cookie: str
    grok_user_agent: str
    grok_browser: str
    grok_proxy: str
    grok_timeout_seconds: float
    log_retention_days: int
    flaresolverr_url: str
    flaresolverr_proxy_url: str


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _get_nested(data: dict, path: str, default: object) -> object:
    cur: object = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _str(data: dict, path: str, default: str) -> str:
    raw = _get_nested(data, path, default)
    return str(raw).strip() if raw is not None else default


def _int(data: dict, path: str, default: int) -> int:
    try:
        return int(_get_nested(data, path, default))
    except (TypeError, ValueError):
        return default


def _float(data: dict, path: str, default: float) -> float:
    try:
        return float(_get_nested(data, path, default))
    except (TypeError, ValueError):
        return default


def load_settings() -> Settings:
    data = _read_toml(CONFIG_PATH)
    return Settings(
        server_host=_str(data, "server.host", "0.0.0.0"),
        server_port=_int(data, "server.port", 8024),
        api_key=_str(data, "auth.api_key", "change-me"),
        grok_cookie=_str(data, "grok.cookie", ""),
        grok_user_agent=_str(data, "grok.user_agent", _DEFAULT_UA),
        grok_browser=_str(data, "grok.browser", "chrome142"),
        grok_proxy=_str(data, "grok.proxy", ""),
        grok_timeout_seconds=_float(data, "grok.timeout_seconds", 120.0),
        log_retention_days=_int(data, "log.retention_days", 90),
        flaresolverr_url=_str(data, "grok.flaresolverr_url", ""),
        flaresolverr_proxy_url=_str(data, "grok.flaresolverr_proxy_url", ""),
    )


def save_settings(settings: Settings, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            "[server]",
            f"host = {json.dumps(settings.server_host, ensure_ascii=False)}",
            f"port = {settings.server_port}",
            "",
            "[auth]",
            f"api_key = {json.dumps(settings.api_key, ensure_ascii=False)}",
            "",
            "[grok]",
            f"cookie = {json.dumps(settings.grok_cookie, ensure_ascii=False)}",
            f"user_agent = {json.dumps(settings.grok_user_agent, ensure_ascii=False)}",
            f"browser = {json.dumps(settings.grok_browser, ensure_ascii=False)}",
            f"proxy = {json.dumps(settings.grok_proxy, ensure_ascii=False)}",
            f"timeout_seconds = {settings.grok_timeout_seconds}",
            f"flaresolverr_url = {json.dumps(settings.flaresolverr_url, ensure_ascii=False)}",
            f"flaresolverr_proxy_url = {json.dumps(settings.flaresolverr_proxy_url, ensure_ascii=False)}",
            "",
            "[log]",
            f"retention_days = {settings.log_retention_days}",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


class SettingsStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = RLock()

    def get(self) -> Settings:
        with self._lock:
            return self._settings

    def update(self, **kwargs: object) -> Settings:
        allowed = set(Settings.__dataclass_fields__)
        patch = {key: value for key, value in kwargs.items() if key in allowed and value is not None}
        with self._lock:
            self._settings = replace(self._settings, **patch)
            save_settings(self._settings)
            return self._settings


def mask_secret(value: str, *, keep: int = 6) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "***"
    return f"{text[:keep]}...{text[-keep:]}"
