"""uPic MCP Server — image upload via the installed uPic.app.

Tools exposed:
  * upload_image            — upload a local file (any absolute path; auto-staged if sandboxed)
  * upload_image_from_base64 — upload raw base64 image data (for agent-generated screenshots)
  * list_hosts              — list hosts configured in uPic
  * get_default_host        — show the currently selected default host
  * uploader_info           — runtime info (binary path, compress factor, default host)

Design goals:
  1. Zero modification to uPic source — we only invoke its existing CLI.
  2. Inherit uPic settings 1:1 (compression, default host, output format) via UserDefaults.
  3. Bypass uPic's sandbox path whitelist by staging files into ~/.upic-staging/.
  4. Return a single clean URL in MCP tool results (not the full verbose stdout).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# --- constants ---------------------------------------------------------------

# Location of the uPic binary. Overridable with the UPIC_BINARY env var so that
# users with a non-standard install (e.g. developer signing, /Applications
# moved) can still run this server without patching the source.
UPIC_BINARY = os.environ.get(
    "UPIC_BINARY", "/Applications/uPic.app/Contents/MacOS/uPic"
)
UPIC_BUNDLE_ID = "com.svend.uPic.macos"
STAGING_DIR = Path.home() / ".upic-staging"
# Paths uPic's sandbox has valid bookmarks for. Anything outside gets staged.
SANDBOX_OK_PREFIXES = (
    str(Path.home()),
    "/Users/",
    "/Applications/",
    "/System/",
    "/opt/",
    "/Volumes/",
    "/cores/",
)
UPLOAD_TIMEOUT_SEC = 120

mcp = FastMCP("upic")

# --- helpers -----------------------------------------------------------------


@dataclass
class UploadResult:
    url: str
    source: str            # original path/description
    staged_from: str | None  # set when we copied the file into staging
    size_bytes: int
    host_name: str | None  # uPic's default host at upload time
    compress_factor: int   # 100 == off, <100 == compressed
    elapsed_ms: int


def _read_upic_defaults() -> dict[str, Any]:
    """Read uPic's UserDefaults plist directly.

    Preferred over `defaults read` because we need structured types (int, array)
    and this way we don't fight shell escaping on the host-item JSON strings.
    """
    candidates = [
        Path.home() / "Library/Containers" / UPIC_BUNDLE_ID / "Data/Library/Preferences" / f"{UPIC_BUNDLE_ID}.plist",
        Path.home() / "Library/Preferences" / f"{UPIC_BUNDLE_ID}.plist",
    ]
    for p in candidates:
        if p.is_file():
            try:
                with p.open("rb") as f:
                    return plistlib.load(f)
            except Exception:
                continue
    # Fallback to `defaults export` (always works, even with cfprefsd caching).
    try:
        out = subprocess.check_output(
            ["defaults", "export", UPIC_BUNDLE_ID, "-"],
            timeout=5,
        )
        return plistlib.loads(out)
    except Exception:
        return {}


def _parse_hosts(defaults: dict[str, Any]) -> list[dict[str, Any]]:
    """uPic stores each host as a JSON-encoded string inside an array."""
    raw = defaults.get("uPic_hostItems") or []
    hosts: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        try:
            host = json.loads(item)
        except json.JSONDecodeError:
            continue
        # Hide secrets from tool output but keep enough for humans to identify.
        hosts.append(
            {
                "id": str(host.get("id", "")),
                "name": host.get("name", ""),
                "type": host.get("type", ""),
            }
        )
    return hosts


def _get_default_host_info() -> dict[str, Any] | None:
    d = _read_upic_defaults()
    default_id = d.get("uPic_DefaultHostId")
    if not default_id:
        return None
    for host in _parse_hosts(d):
        if host["id"] == str(default_id):
            return host
    return None


def _get_compress_factor() -> int:
    d = _read_upic_defaults()
    factor = d.get("uPic_CompressFactor")
    if factor is None:
        return 100
    try:
        return int(factor)
    except (TypeError, ValueError):
        return 100


def _needs_staging(path: Path) -> bool:
    """Return True if uPic's sandbox probably can't read this path directly."""
    resolved = str(path.resolve())
    return not any(resolved.startswith(p) for p in SANDBOX_OK_PREFIXES)


def _stage_file(src: Path) -> Path:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    # Content-hash prefix so repeat uploads dedupe and we don't clash on same filename.
    h = hashlib.sha1()
    with src.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    short = h.hexdigest()[:8]
    dest = STAGING_DIR / f"{short}_{src.name}"
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest


def _extract_url(stdout: str) -> str | None:
    """Extract uPic's uploaded URL from CLI stdout.

    uPic CLI stdout looks like::

        共 1 个文件路径和链接
        Uploading ...
        Uploading 1/1
        Output URL:
        https://img.aws.xin/uPic/foo.png

    Return the first non-empty line after the ``Output URL:`` marker. That
    line is usually a URL (``http://`` / ``https://``). When upload failed and
    the CLI was not running in ``--slient`` mode, the line instead contains a
    human-readable error message (e.g. ``"Invalid file path"``). We return the
    raw line either way; the caller (``_upload_path``) is responsible for
    distinguishing the two cases and raising an informative error when the
    line is not a URL.
    """
    lines = [l.rstrip() for l in stdout.splitlines()]
    found_marker = False
    for line in lines:
        if found_marker:
            stripped = line.strip()
            if not stripped:
                continue
            # Whether it's a URL or an error message, return the line verbatim
            # and let the caller decide what to do.
            return stripped
        if line.startswith("Output URL:"):
            found_marker = True
    return None


def _run_upic(paths: list[str]) -> tuple[str, str]:
    """Invoke the uPic CLI. Returns (stdout, stderr)."""
    cmd = [UPIC_BINARY, "-u", *paths, "-o", "url"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=UPLOAD_TIMEOUT_SEC,
    )
    return proc.stdout, proc.stderr


def _upload_path(path: Path) -> UploadResult:
    if not path.is_file():
        raise ValueError(f"not a regular file: {path}")

    staged_from: str | None = None
    upload_path = path

    # If the file is already inside our staging directory, don't stage it
    # again — that would produce a double-prefixed file (``hash_hash_name``)
    # and leak copies. This matters when STAGING_DIR itself lives outside the
    # sandbox whitelist (only happens in tests via monkeypatch, but still a
    # correctness invariant worth enforcing).
    try:
        staging_resolved = str(STAGING_DIR.resolve())
        path_resolved = str(path.resolve())
        already_in_staging = (
            path_resolved == staging_resolved
            or path_resolved.startswith(staging_resolved + os.sep)
        )
    except (OSError, ValueError):
        already_in_staging = False

    if not already_in_staging and _needs_staging(path):
        staged_from = str(path)
        upload_path = _stage_file(path)

    size = upload_path.stat().st_size
    host = _get_default_host_info()
    factor = _get_compress_factor()

    started = time.monotonic()
    stdout, stderr = _run_upic([str(upload_path)])
    elapsed_ms = int((time.monotonic() - started) * 1000)

    url = _extract_url(stdout)
    if not url or not url.startswith(("http://", "https://")):
        msg = (url or "unknown error").strip() or "unknown error"
        raise RuntimeError(
            f"uPic upload failed: {msg}\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
        )

    return UploadResult(
        url=url,
        source=str(path),
        staged_from=staged_from,
        size_bytes=size,
        host_name=(host or {}).get("name") if host else None,
        compress_factor=factor,
        elapsed_ms=elapsed_ms,
    )


def _result_to_dict(r: UploadResult) -> dict[str, Any]:
    return {
        "url": r.url,
        "source": r.source,
        "staged_from": r.staged_from,
        "size_bytes": r.size_bytes,
        "host": r.host_name,
        "compress_factor": r.compress_factor,
        "compression_enabled": r.compress_factor < 100,
        "elapsed_ms": r.elapsed_ms,
    }


# --- MCP tools ---------------------------------------------------------------


@mcp.tool()
def upload_image(path: str) -> dict[str, Any]:
    """Upload a local image/file via uPic and return the CDN URL.

    Args:
        path: Absolute path to a file on disk. Paths outside uPic's sandbox
              whitelist (e.g. /tmp/*) are auto-copied to ~/.upic-staging/ first.

    Returns a dict with ``url``, ``size_bytes``, ``host``, ``compress_factor``
    (<100 means uPic's built-in JPG/PNG compression kicked in), and
    ``staged_from`` if the file had to be copied.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"file not found: {path}"}
    result = _upload_path(p)
    return _result_to_dict(result)


@mcp.tool()
def upload_image_from_base64(
    data: str,
    filename: str = "image.png",
) -> dict[str, Any]:
    """Upload raw base64-encoded image bytes via uPic.

    Useful for uploading screenshots or generated images you have in memory
    without writing them to disk first. The data is decoded to a temp file
    in ``~/.upic-staging/`` and passed to uPic.

    Args:
        data: Base64-encoded bytes. ``data:image/png;base64,`` prefix is OK.
        filename: Filename hint (controls extension / CDN key). Only the
            basename is used — any path components are stripped to prevent
            directory traversal (``../../etc/passwd`` becomes ``passwd``).
            Defaults to ``image.png``.
    """
    # Strip data-URL prefix if the caller pasted one.
    if "," in data and data.lstrip().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception as e:
        return {"error": f"invalid base64: {e}"}
    if not raw:
        return {"error": "empty payload"}

    # Sanitize filename: only allow the basename, strip path components and
    # any separators. Empty / dotted-only filenames fall back to a safe default.
    safe_name = Path(filename).name if filename else ""
    safe_name = safe_name.lstrip(".")  # block ".hidden" or ".."
    if not safe_name:
        safe_name = "image.png"

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    # Prefix with short content hash so repeated uploads of same bytes dedupe.
    h = hashlib.sha1(raw).hexdigest()[:8]
    tmp = STAGING_DIR / f"{h}_{safe_name}"
    tmp.write_bytes(raw)

    try:
        result = _upload_path(tmp)
    except Exception:
        # We created this staging file ourselves (not user-supplied), so on
        # failure clean it up to avoid orphaning bytes in ~/.upic-staging/.
        tmp.unlink(missing_ok=True)
        raise
    return _result_to_dict(result)


@mcp.tool()
def list_hosts() -> dict[str, Any]:
    """List all hosts configured in uPic (names and types, no secrets)."""
    d = _read_upic_defaults()
    hosts = _parse_hosts(d)
    default_id = str(d.get("uPic_DefaultHostId", ""))
    for h in hosts:
        h["is_default"] = h["id"] == default_id
    return {"hosts": hosts, "count": len(hosts)}


@mcp.tool()
def get_default_host() -> dict[str, Any]:
    """Return the uPic host currently selected as the default upload target."""
    host = _get_default_host_info()
    if host is None:
        return {"error": "no default host set in uPic"}
    return host


@mcp.tool()
def uploader_info() -> dict[str, Any]:
    """Runtime info: uPic binary, compression setting, default host, staging dir."""
    binary_ok = Path(UPIC_BINARY).is_file()
    host = _get_default_host_info()
    factor = _get_compress_factor()
    return {
        "upic_binary": UPIC_BINARY,
        "upic_binary_exists": binary_ok,
        "staging_dir": str(STAGING_DIR),
        "default_host": host,
        "compress_factor": factor,
        "compression_enabled": factor < 100,
    }


# --- entrypoint --------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
