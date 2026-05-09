# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-09

Initial public release. Upload-only MVP. Tested end-to-end against the real uPic.app and a live S3/R2 host.

### Added

- **MCP server** (`server.py`, ~340 LOC) built on FastMCP (stdio transport) exposing five tools:
  - `upload_image(path)` — upload a local file, auto-staging paths outside uPic's sandbox whitelist to `~/.upic-staging/` via SHA-1 content hash dedup.
  - `upload_image_from_base64(data, filename?)` — upload raw base64 bytes (accepts `data:image/...;base64,` prefix).
  - `list_hosts()` — enumerate configured uPic hosts with `is_default` flag; secrets redacted.
  - `get_default_host()` — return the currently selected default host.
  - `uploader_info()` — runtime diagnostics (binary path, quality setting, staging dir, default host).
- **Sandbox path workaround** — detects files outside the `[$HOME, /Users/, /Applications/, /System/, /opt/, /Volumes/, /cores/]` whitelist and transparently stages them into `~/.upic-staging/<sha1_prefix>_<filename>`. Idempotent: re-uploading identical content reuses the existing staged file.
- **Automatic configuration inheritance** — reads uPic's `UserDefaults` plist at `~/Library/Containers/com.svend.uPic.macos/Data/Library/Preferences/com.svend.uPic.macos.plist` so the default host and quality factor always match the menu bar UI.
- **Compression parity with UI** — uploads flow through `BaseUploaderUtil.compressImage`, so whatever quality the user set in "Compress images before uploading" is applied to MCP uploads too. Reported back as `quality_factor` (1–100).
- **Structured results** — tool responses include `{url, source, staged_from, size_bytes, host, quality_factor, compression_enabled, elapsed_ms}`.
- **Honest error surfacing** — CLI failure includes stdout and stderr in the `RuntimeError` for agent self-diagnosis.
- **Three-layer test suite** (44 tests total, all passing):
  - 21 unit tests covering `_extract_url`, `_parse_hosts`, `_needs_staging`, `_result_to_dict` with edge cases.
  - 17 integration tests with `_run_upic` and `_read_upic_defaults` mocked, exercising full tool paths: staging triggers, content-hash dedup, tilde expansion, missing file errors, CLI failure propagation, `data:` URL prefix stripping.
  - 6 end-to-end tests (opt-in via `-m e2e`) that generate unique PNGs, invoke the real uPic CLI, and verify each CDN URL via `httpx.get`.
- **pytest configuration** in `pyproject.toml` with an `e2e` marker; default `addopts` excludes e2e so offline runs are fast.
- **README** with Quick Start, architecture diagram, CLI comparison, compression explanation, troubleshooting, and roadmap.
- **LICENSE** (MIT).
- **`.gitignore`** covering Python / uv / macOS / editor artifacts.

### Verified

- Uploaded a 3100×1826 PNG (0.97 MB) successfully to `https://img.aws.xin/uPic/*` with `quality_factor=90`, producing a 0.57 MB file (41.6% smaller). Visual comparison showed no perceptible degradation.
- Automatic staging from `/tmp/*` paths works end-to-end.
- All 44 tests pass locally against a live S3/R2 host.
- MCP server is registered in Kiro (`~/.kiro/settings/mcp.json`) and discoverable via `kiro-cli mcp list`.
- stdio protocol smoke test (`initialize` + `tools/list`) returns all 5 tools and exits cleanly.

### Dependencies

- `mcp[cli]>=1.2.0`
- Python 3.11+
- Dev: `pytest>=9.0.3`, `pytest-cov>=7.1.0`, `httpx>=0.28.1`

### Known Limitations

- No way to select a non-default host per-call (requires a small patch to uPic's `Cli.swift`; planned in roadmap).
- No way to override `quality_factor` per-call (same reason).
- No `set_default_host` / `set_compress_factor` tools yet (planned).
- No history/search tools yet (WCDB reader planned).
- macOS only. Linux/Windows not supported because uPic is a macOS app.

[Unreleased]: https://github.com/neosun100/upic-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/neosun100/upic-mcp/releases/tag/v0.1.0
