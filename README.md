# upic-mcp

> **[uPic](https://github.com/gee1k/uPic) for humans — and for AI agents.**
> A Model Context Protocol (MCP) server that exposes the installed **uPic.app** (macOS) as structured tools, so Claude / Kiro / Cursor can upload images and receive stable CDN URLs without ever touching the menu bar.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-1.2+-green.svg)](https://modelcontextprotocol.io)
[![Tests: 60 passed](https://img.shields.io/badge/tests-60%20passed-brightgreen.svg)](#testing)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](#)

## Why

uPic is the best image-upload tool on macOS (12+ image hosts, auto-compression, screenshot integration). But it's a menu-bar app — there's no good way for an AI agent or a headless script to ask it "please upload this file and give me the URL back". The built-in CLI exists but is limited: sandbox path restrictions, verbose stdout, no base64 input, no structured output.

**upic-mcp fixes that** without modifying a single line of uPic's source code. It's a thin, correct wrapper that:

- Reuses uPic's existing CLI (`/Applications/uPic.app/Contents/MacOS/uPic`)
- Inherits all UI settings automatically (default host, compression quality, save-key path) via UserDefaults
- Bypasses the sandbox path whitelist by content-hash staging to `~/.upic-staging/`
- Returns structured JSON (`{url, size_bytes, host, quality_factor, ...}`) instead of scraped stdout
- Adds first-class support for base64 input (for agent-generated screenshots)

## Features

Five MCP tools, production-tested with 44 automated tests:

| Tool | Description |
|---|---|
| **`upload_image(path)`** | Upload a local file. Paths outside uPic's sandbox (e.g. `/tmp/*`) are auto-staged to `~/.upic-staging/` via SHA-1 content hash dedup. Returns `{url, size_bytes, host, quality_factor, ...}`. |
| **`upload_image_from_base64(data, filename?)`** | Upload raw base64-encoded bytes (accepts `data:image/png;base64,` prefix). Perfect for AI-generated screenshots you have in memory but not on disk. |
| **`list_hosts()`** | Returns all uPic hosts (Qiniu/Imgur/S3/SMMS/etc.) with an `is_default` flag. Secrets are never exposed. |
| **`get_default_host()`** | Returns the currently selected default host. |
| **`uploader_info()`** | Runtime diagnostics: binary path, UI quality setting, staging dir, default host. |

## Highlights

- 🔐 **Zero uPic modification** — your installed uPic.app is untouched. Uninstall this project, zero trace.
- 🌍 **Works with all 12 uPic hosts** — Qiniu KODO / UPYUN / Aliyun OSS / Tencent COS / Amazon S3 / Imgur / GitHub / Gitee / SMMS / Weibo / Baidu BOS / custom.
- 🗜 **Compression is automatic** — whatever you set in the UI (e.g. "Compress to 90% quality") is applied to MCP uploads too, because both paths go through `BaseUploaderUtil.compressImage`.
- 📦 **Sandbox-safe** — uPic's sandbox can only read files under a whitelisted set of prefixes. This server detects that and transparently stages out-of-whitelist files.
- 🛡 **Security-hardened** — filename path traversal is blocked (basename sanitization), staging files are auto-cleaned on upload failure, and secrets never appear in tool output. See the [Security](#security) section for the full threat model.
- 🧪 **Well-tested** — 34 unit + 17 integration + 9 end-to-end tests (60 total). Default `pytest` runs in ~0.5s fully offline; `pytest -m e2e` runs 9 real round-trip uploads in ~30s.
- 🪶 **Zero-config** — reads your existing uPic configuration directly from `~/Library/Containers/com.svend.uPic.macos/Data/Library/Preferences/com.svend.uPic.macos.plist`.

## Requirements

- macOS (tested on macOS 26.4 Tahoe, Apple Silicon & Intel)
- [uPic.app](https://apps.apple.com/cn/app/id1549159979) installed in `/Applications/` and logged into at least one host
- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) for dependency management
- An MCP-capable client (Kiro, Claude Desktop, Cursor, …)

## Quick Start

### 1. Install

```bash
git clone https://github.com/neosun100/upic-mcp.git
cd upic-mcp
uv sync
```

### 2. Register with your MCP client

**Kiro** (`~/.kiro/settings/mcp.json`):

```json
{
  "mcpServers": {
    "upic": {
      "command": "/absolute/path/to/upic-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/upic-mcp/server.py"]
    }
  }
}
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "upic": {
      "command": "/absolute/path/to/upic-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/upic-mcp/server.py"]
    }
  }
}
```

### 3. Use it

Restart the client, then simply ask the agent to upload an image:

> "Upload `~/Desktop/screenshot.png` and give me the URL."

The agent calls `@upic/upload_image` and you get back:

```json
{
  "url": "https://img.aws.xin/uPic/screenshot.png",
  "size_bytes": 595375,
  "host": "Amazon S3",
  "quality_factor": 90,
  "staged_from": null,
  "elapsed_ms": 2131
}
```

## Example Output

Real upload of a 3100×1826 screenshot (0.97 MB) through the default S3/R2 host:

```
URL:             https://img.aws.xin/uPic/Xnip2026-04-21_22-07-56.png
Original size:   1,019,291 bytes (0.97 MB)
Uploaded size:     595,375 bytes (0.57 MB)
Size reduction:      41.6% (via libminipng quality-90 lossy PNG)
HTTP status:     200 OK, content-type: image/png
Visual quality:  no visible degradation
```

The 41.6% size reduction is the compression effect of uPic's `quality=90` setting. Lower the quality in the uPic menu bar to get smaller files at the cost of visible artifacts.

## How It Works

<p align="center">
  <img src="https://img.aws.xin/uPic/architecture.png" alt="upic-mcp 4-layer architecture: MCP Client → upic-mcp → uPic.app CLI → Image Host" style="width:100%;max-width:720px" />
</p>

Four layers, each with a clear single responsibility:

1. **MCP Client** (Kiro / Claude Desktop / Cursor) sends `@upic/upload_image(path)` over JSON-RPC on stdio.
2. **upic-mcp** (this project) reads uPic's `UserDefaults` plist, stages the path if it's outside the sandbox whitelist, then invokes uPic's CLI via `subprocess.run`.
3. **uPic.app CLI** (untouched) reads the config, runs the file through `BaseUploaderUtil.compressImage` (PNG → `libminipng`; JPG → `NSBitmapImageRep`), picks the matching adapter from 12 host types (S3, Qiniu, Imgur, …), uploads over HTTPS, and prints the final URL.
4. **Image Host** returns a stable CDN URL like `https://img.aws.xin/uPic/...` that flows back up the chain.

### Sandbox Workaround

uPic is a Sandboxed app. Its CLI mode can only read files under paths for which it has valid Security-Scoped Bookmarks. Everything else returns `isReadableFile: false` and the upload silently no-ops.

<p align="center">
  <img src="https://img.aws.xin/uPic/sandbox-flow.png" alt="Sandbox workaround flowchart: whitelist check → direct upload or SHA-1 content-hash staging" style="width:100%;max-width:720px" />
</p>

**This server's workaround in one paragraph:** resolve the caller's path, check it against `[$HOME, /Users/, /Applications/, /System/, /opt/, /Volumes/, /cores/]`. If it's on the whitelist, pass the path unchanged. If not, compute SHA-1 of file contents, copy it to `~/.upic-staging/<sha1prefix>_<filename>`, and hand the staged path to uPic. Staging is idempotent — re-uploading identical content reuses the existing file rather than creating duplicates.

## CLI Usage (uPic Built-in)

For completeness, here's how to use uPic's native CLI directly (without MCP):

```bash
# Basic upload, URL returned on stdout
/Applications/uPic.app/Contents/MacOS/uPic -u ~/foo.png -o url

# Markdown format
/Applications/uPic.app/Contents/MacOS/uPic -u ~/foo.png -o md
# → ![foo](https://img.aws.xin/uPic/foo.png)

# Multiple files
/Applications/uPic.app/Contents/MacOS/uPic -u ~/a.png ~/b.png ~/c.png -o url

# HTML / UBB formats
/Applications/uPic.app/Contents/MacOS/uPic -u ~/foo.png -o html
/Applications/uPic.app/Contents/MacOS/uPic -u ~/foo.png -o ubb
```

**Limitations of the native CLI** (which `upic-mcp` fixes):

- `/tmp/*` paths fail silently (sandbox)
- No structured output (you must regex the stdout)
- No base64 input
- No way to query configured hosts or default host
- Multi-line output mixed with status messages makes piping awkward

## Understanding uPic's Compression

The `quality_factor` field in tool results maps to uPic's **"Compress images before uploading"** menu. It's a **quality** setting, not a size-reduction percentage:

| UI display | Meaning | Typical size reduction on a 1 MB PNG |
|---|---|---|
| **Off** (100) | No re-encoding | 0% |
| 90 | Preserve 90% quality | 20–45% |
| 70 | Preserve 70% quality | 40–65% |
| 50 | Preserve 50% quality | 60–80% |
| 30 | Preserve 30% quality | 75–90% |
| 10 | Preserve 10% quality (heavy loss) | 85–95% |

- PNGs run through [`libminipng`](https://github.com/ibireme/MiniPNG) (pngquant family, lossy palette quantization)
- JPGs use `NSBitmapImageRep.compressionFactor`
- Other formats (GIF, WebP, SVG, PDF, …) pass through uncompressed

## Configuration

The server ships with zero-config by design — it reads everything from your
existing uPic UI settings. For non-standard installs, one environment variable
is supported:

| Variable | Default | Effect |
|---|---|---|
| `UPIC_BINARY` | `/Applications/uPic.app/Contents/MacOS/uPic` | Path to the uPic executable. Set this if you built uPic from source, moved the app to a non-standard location, or are running an unsigned developer build. |

Set it in your MCP client's server config under `env`:

```json
{
  "mcpServers": {
    "upic": {
      "command": "/path/to/upic-mcp/.venv/bin/python",
      "args": ["/path/to/upic-mcp/server.py"],
      "env": {
        "UPIC_BINARY": "/Users/me/build/uPic.app/Contents/MacOS/uPic"
      }
    }
  }
}
```

All other settings (default host, image quality, save-key path, output format)
come from uPic's UI directly. Change them in the menu bar app and the next MCP
upload picks them up automatically — no server restart needed.

## Security

This server runs locally and never opens a listening socket. Security
considerations focus on three threat surfaces:

### 1. Filename path traversal (mitigated)

`upload_image_from_base64` accepts an optional `filename` argument that
controls the local staging path. The filename is sanitized to a **basename
only** (`Path(filename).name` + leading-dot stripping) so calls like:

```json
{"data": "...", "filename": "../../etc/passwd"}
```

cannot write outside `~/.upic-staging/`. The attempt becomes a staged file
named `passwd`, not a traversal. Empty / dotted-only filenames fall back to
`image.png`. Covered by 4 unit tests and 1 end-to-end test.

### 2. Command injection (not applicable)

The uPic CLI is invoked via `subprocess.run(cmd, ...)` with `cmd` as a **list**
of arguments — no shell is involved. File paths and filenames flow through
as opaque argv values; shell metacharacters (`;`, `` ` ``, `$(…)`, `|`, etc.)
cannot be interpreted. No `shell=True` appears anywhere in the codebase.

### 3. Secrets exposure (mitigated)

uPic stores host credentials (access keys, secret keys, client IDs) in its
`UserDefaults` plist. The `list_hosts` tool strips the `data` field entirely
before returning, so only `id`, `name`, and `type` are ever exposed. A unit
test asserts that known-secret markers cannot appear in the tool's output.

### Additional hardening

- **Staging cleanup on failure.** When `upload_image_from_base64` fails, the
  staging file we created is unlinked so bytes don't accumulate in
  `~/.upic-staging/`. User-supplied file paths are never modified or deleted,
  even on failure — an integration test pins this invariant.
- **Subprocess timeout.** Every `_run_upic` call has a 120-second timeout;
  a hung uPic CLI cannot block the MCP server indefinitely.
- **No network code.** The MCP server itself never opens a socket. All
  outbound traffic is uPic's own HTTPS upload to the image host.

### Filesystem footprint

```
~/.upic-staging/            Staging directory (auto-created, content-hash dedup)
~/.kiro/settings/mcp.json   Where Kiro learns about this server (you add the entry)
```

The server never writes anywhere else on disk. Uninstalling is just deleting
the `upic-mcp/` folder and the `~/.upic-staging/` directory.

## Testing

<p align="center">
  <img src="https://img.aws.xin/uPic/test-pyramid.png" alt="Three-layer test pyramid: 34 unit + 17 integration + 9 e2e = 60 tests total" style="width:100%;max-width:720px" />
</p>

```bash
# Fast, offline, runs in ~0.5s
uv run pytest                      # 51 passed (9 e2e deselected)

# Real uploads to your CDN, ~30s
uv run pytest -m e2e -v            # 9 passed

# Everything
uv run pytest --override-ini="addopts=-ra" -v   # 60 passed
```

### Test layers

| File | Count | Runtime | Hits network |
|---|---|---|---|
| [`tests/test_unit.py`](./tests/test_unit.py) | 34 | ~0.3s | No |
| [`tests/test_integration.py`](./tests/test_integration.py) | 17 | ~0.3s | No (subprocess + plist mocked) |
| [`tests/test_e2e.py`](./tests/test_e2e.py) | 9 | ~30s | **Yes** — real uPic CLI, real CDN upload, real HTTP `GET` |

E2E tests are opt-in via `-m e2e` so CI can run the fast tests without needing uPic or network access.

## Project Layout

```
upic-mcp/
├── server.py               # FastMCP server, 5 tools (~340 LOC)
├── tests/
│   ├── test_unit.py         # Pure-function unit tests
│   ├── test_integration.py  # Tool-level tests with subprocess mocked
│   └── test_e2e.py          # End-to-end real-upload tests
├── docs/                   # Architecture diagrams (SVG sources + PNG renders)
│   ├── architecture.svg     # 4-layer system architecture
│   ├── architecture.png
│   ├── sandbox-flow.svg     # Sandbox path workaround flowchart
│   ├── sandbox-flow.png
│   ├── test-pyramid.svg     # Three-layer test pyramid
│   └── test-pyramid.png
├── pyproject.toml          # uv-managed, with pytest markers
├── uv.lock
├── README.md
├── CHANGELOG.md
└── LICENSE                 # MIT
```

## Troubleshooting

<details>
<summary><strong>Server not appearing in `kiro-cli mcp list`</strong></summary>

Verify `~/.kiro/settings/mcp.json` contains a `mcpServers.upic` entry with absolute paths to `.venv/bin/python` and `server.py`. Both files must exist.
</details>

<details>
<summary><strong>Upload returns empty URL / "Invalid file path"</strong></summary>

uPic's sandbox blocked the source file. Either:
- Move the file to a whitelisted location (`~/`, `/Users/`, `/Applications/`, `/System/`, `/opt/`, `/Volumes/`, `/cores/`), or
- Let the server auto-stage it — if you pass `/tmp/*` the server should do this automatically. If auto-staging fails, grant uPic **Full Disk Access** in System Settings → Privacy & Security.
</details>

<details>
<summary><strong>Upload works in UI but not via MCP</strong></summary>

Both share the same `UserDefaults`, so the default host is identical. Check `~/Library/Containers/com.svend.uPic.macos/Data/Library/Logs/uPic/*.log` — the CLI invocation writes to the same log file as the UI.
</details>

<details>
<summary><strong>Only 40% size reduction with `quality_factor = 90`?</strong></summary>

That's expected. `quality_factor` is a **quality** setting, not a size-reduction ratio. See the [compression table above](#understanding-upics-compression). If you want >90% size reduction, set the UI to quality 10–30 (but expect visible artifacts).
</details>

<details>
<summary><strong>Tests pass locally, CI fails on e2e</strong></summary>

E2E tests require uPic.app to be installed and at least one host configured. CI should run `uv run pytest` (without `-m e2e`), which excludes the e2e suite via the default `addopts` in `pyproject.toml`.
</details>

<details>
<summary><strong>Two uPic icons in the menu bar after development / testing</strong></summary>

This happens when uPic is accidentally launched without a `-u` argument — the CLI branch only activates for commands like `uPic -u file.png -o url`. Anything else (including `uPic --help`, `uPic --version`, or a bare `uPic`) falls through to UI mode and spawns a second menu-bar instance.

**MCP uploads never cause this** — `server.py` always invokes `uPic` with `-u`. The duplicate usually comes from manually probing the CLI in a terminal.

**Check & cleanup:**

```bash
# How many uPic processes are running?
ps aux | grep -iE "/uPic" | grep -v grep

# Should be 1. If more, identify the stale PID (older START time or a
# --help-style command) and kill it:
kill -9 <PID>
```

The menu bar refreshes on the next click.
</details>

<details>
<summary><strong>Using a non-standard uPic.app location</strong></summary>

Set the `UPIC_BINARY` environment variable in the server's MCP config:

```json
{
  "upic": {
    "command": "/path/to/.venv/bin/python",
    "args": ["/path/to/server.py"],
    "env": {
      "UPIC_BINARY": "/Users/me/build/uPic.app/Contents/MacOS/uPic"
    }
  }
}
```

Also see the [Configuration](#configuration) section.
</details>

## Roadmap

The MVP upload pipeline is production-hardened with 60 passing tests. Planned
expansion in rough priority order:

- [x] ~~Upload by path, base64, and automatic sandbox staging~~ (v0.1.0)
- [x] ~~`list_hosts`, `get_default_host`, `uploader_info`~~ (v0.1.0)
- [x] ~~Environment-variable override for `UPIC_BINARY`~~ (hardening pass)
- [x] ~~Path-traversal protection for base64 filenames~~ (hardening pass)
- [ ] `set_default_host(name_or_id)` — switch uPic's default host from agent
- [ ] `set_compress_factor(factor)` — change quality without the menu bar
- [ ] `get_upload_history(limit, host?)` — read the WCDB history database directly
- [ ] `upload_from_clipboard()` — bypass uPic's `uploadByPasteboard` and grab `NSPasteboard` directly
- [ ] `copy_last_url()` — push the most recent upload URL to system pasteboard
- [ ] `delete_upload(url)` — per-host delete where the host supports it (S3, Imgur, …)
- [ ] Native `upic` CLI wrapper (shell shim + Python fallback) that mirrors all MCP tools from the terminal
- [ ] Optional `--host` flag support via a small patch to uPic's `Cli.swift` (upstream PR)

If any of these are blocking your use case, open an issue.

## Design Philosophy

1. **Never modify uPic's source.** Wrap it correctly. Your installed app must stay upstream-clean so security updates and App Store releases keep working.
2. **Inherit UI configuration 1:1.** Don't maintain a separate config. Whatever the user picked in the menu bar is the source of truth.
3. **Keep the tool surface minimal.** Five tools is enough for the upload use case. Resist scope creep until real user feedback shows a gap.
4. **Be honest with errors.** Return structured errors (`{error: ...}`) rather than raising cryptic exceptions. Include stdout/stderr so the agent can self-diagnose.
5. **Test every layer.** Unit → integration → e2e. Nothing ships without passing all three.

## Contributing

Issues and PRs welcome. Please:

1. Open an issue first for non-trivial changes
2. Run the full test suite before submitting: `uv run pytest --override-ini="addopts=-ra"`
3. For new tools, add tests in all three layers (unit + integration + e2e)
4. Follow existing code style; no new dependencies without discussion

## Related

- [uPic](https://github.com/gee1k/uPic) — the excellent macOS image uploader this project wraps
- [Model Context Protocol](https://modelcontextprotocol.io) — the MCP specification
- [FastMCP](https://github.com/modelcontextprotocol/python-sdk) — the Python SDK used by this server

## License

[MIT](./LICENSE) © 2026 [Neo 孫](https://github.com/neosun100)

uPic itself is MIT-licensed by [Svend Jin](https://github.com/gee1k). This project is an independent wrapper and not affiliated with the upstream uPic project.
