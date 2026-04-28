# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-04-28

Initial public release.

### Added
- OpenAI-compatible REST endpoints: `/v1/chat/completions`, `/v1/images/generations`, `/v1/videos/generate`, `/v1/models`
- Single-page Web UI: chat, continuous image gen, task queue, gallery, files, logs, settings
- `curl_cffi` chrome142 TLS fingerprint impersonation
- FlareSolverr-based `cf_clearance` auto-refresh (session_keeper, ~10 min interval)
- SQLite request logs (chat / image / video) with retention cleanup
- cURL / HAR import for one-click cookie configuration
- Per-session image gallery + waterfall layout, PhotoSwipe lightbox
- Click-to-fullscreen video modal across feed / gallery / files
- Grok cloud Files browser (waterfall + infinite scroll)
- Sync `grok_browser` with FlareSolverr UA major version automatically
- Pure-TOML configuration (no env-var lookups)
- Multi-arch Docker image (linux/amd64 + linux/arm64)

### Security
- Upstream 401 rewritten to 502 `upstream_unauthorized` to avoid false logout
- All cookies masked in admin / logs responses
- `data/config/mini.toml` and `.env` `.gitignore`-protected
