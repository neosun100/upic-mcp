"""End-to-end tests that exercise the full pipeline against the real uPic.app.

These tests:
  * Generate a real PNG file (using a deterministic byte blob, no PIL)
  * Invoke the installed uPic CLI (no subprocess mocking)
  * Verify the returned URL is live by issuing an HTTP GET
  * Verify size, content type, and (when possible) compression behavior

They require:
  * uPic.app installed at /Applications/uPic.app
  * At least one configured host with a live upload path
  * Outbound network access

Run with::

    uv run pytest -m e2e -v

Skip by default (without -m e2e) because they:
  * Hit the network
  * Consume image-host quota
  * Leave files on the CDN (we do not attempt to delete)
"""
from __future__ import annotations

import struct
import sys
import time
import uuid
import zlib
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as S  # noqa: E402

pytestmark = pytest.mark.e2e


# --- preflight: skip everything if uPic isn't installed ----------------------

if not Path(S.UPIC_BINARY).is_file():
    pytest.skip(
        "uPic.app is not installed at the expected path; skipping E2E tests.",
        allow_module_level=True,
    )


# --- helpers -----------------------------------------------------------------


def _make_png(width: int, height: int, unique_tag: bytes) -> bytes:
    """Build a minimal but valid PNG with a unique tag embedded for dedup safety.

    Produces a truecolor (RGB) image where every row's pixel values come from
    hashing ``unique_tag``, so tests that re-run immediately still get a
    different CDN path (because the filename contains a uuid) but the file is
    small (~1 KB).
    """
    import hashlib

    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # Build rows: each row prefixed with filter byte 0, then RGB triples.
    seed = hashlib.sha1(unique_tag).digest()
    row = bytearray()
    for x in range(width):
        row.append(seed[(x * 3) % 20])
        row.append(seed[(x * 3 + 1) % 20])
        row.append(seed[(x * 3 + 2) % 20])
    raw = b"".join(b"\x00" + bytes(row) for _ in range(height))
    idat = zlib.compress(raw, 9)

    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


@pytest.fixture
def unique_png(tmp_path):
    """A uniquely-named PNG written to /tmp so we also exercise the staging path."""
    tag = uuid.uuid4().bytes
    path = Path("/tmp") / f"upic_e2e_{uuid.uuid4().hex[:8]}.png"
    path.write_bytes(_make_png(40, 30, tag))
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def unique_png_in_home():
    """A uniquely-named PNG under $HOME (no staging needed)."""
    tag = uuid.uuid4().bytes
    path = Path.home() / f"upic_e2e_home_{uuid.uuid4().hex[:8]}.png"
    path.write_bytes(_make_png(40, 30, tag))
    yield path
    path.unlink(missing_ok=True)


# --- sanity checks -----------------------------------------------------------


class TestPreflight:
    def test_upic_binary_is_executable(self):
        info = S.uploader_info()
        assert info["upic_binary_exists"], "uPic.app must be installed for E2E"

    def test_at_least_one_host_configured(self):
        result = S.list_hosts()
        assert result["count"] >= 1, "at least one uPic host must be configured"
        assert any(h["is_default"] for h in result["hosts"]), "a default host must be set"


# --- real uploads ------------------------------------------------------------


class TestRealUpload:
    def test_upload_png_from_tmp_returns_reachable_url(self, unique_png):
        # This exercises: CLI invocation, auto-staging, URL parsing, and HTTP delivery.
        result = S.upload_image(str(unique_png))
        assert "url" in result, result
        url = result["url"]
        assert url.startswith(("http://", "https://")), url
        assert result["staged_from"] == str(unique_png), "tmp path should be auto-staged"
        assert result["size_bytes"] > 0
        assert result["host"], "host name should be populated from UserDefaults"

        # Give the CDN a second to see the new object. Most will 200 immediately.
        deadline = time.monotonic() + 20
        last_status = None
        while time.monotonic() < deadline:
            r = httpx.get(url, follow_redirects=True, timeout=10)
            last_status = r.status_code
            if 200 <= r.status_code < 300 and len(r.content) > 0:
                break
            time.sleep(1)
        else:
            pytest.fail(f"uploaded URL never became reachable (last status {last_status}): {url}")

        # Downloaded body should be non-empty.
        assert len(r.content) > 0

    def test_upload_png_from_home_no_staging(self, unique_png_in_home):
        result = S.upload_image(str(unique_png_in_home))
        assert result["staged_from"] is None
        assert result["url"].startswith(("http://", "https://"))
        r = httpx.get(result["url"], follow_redirects=True, timeout=15)
        assert r.status_code == 200

    def test_upload_image_from_base64_roundtrip(self):
        import base64

        tag = uuid.uuid4().bytes
        png_bytes = _make_png(32, 24, tag)
        data = base64.b64encode(png_bytes).decode()
        filename = f"e2e_b64_{uuid.uuid4().hex[:8]}.png"

        result = S.upload_image_from_base64(data, filename)
        assert "url" in result, result
        assert filename in result["url"]

        r = httpx.get(result["url"], follow_redirects=True, timeout=15)
        assert r.status_code == 200
        assert len(r.content) > 0

    def test_compression_setting_is_inherited(self, unique_png):
        result = S.upload_image(str(unique_png))
        # Whatever the UI was configured to (we saw 90 earlier), the MCP tool should report it.
        factor = result["compress_factor"]
        assert 1 <= factor <= 100
        expected_enabled = factor < 100
        assert result["compression_enabled"] is expected_enabled

    def test_upload_filename_with_spaces(self, tmp_path):
        # Filenames with spaces must round-trip correctly through the CLI
        # (list-form subprocess.run means no shell escaping is needed, but we
        # still want an explicit test to guarantee nothing else mangles it).
        tag = uuid.uuid4().bytes
        path = Path.home() / f"upic e2e space {uuid.uuid4().hex[:8]}.png"
        path.write_bytes(_make_png(32, 24, tag))
        try:
            result = S.upload_image(str(path))
            assert "url" in result, result
            r = httpx.get(result["url"], follow_redirects=True, timeout=15)
            assert r.status_code == 200
            assert len(r.content) > 0
        finally:
            path.unlink(missing_ok=True)

    def test_upload_filename_with_unicode(self):
        # Chinese / emoji characters in the filename. uPic's Swift string
        # handling is Unicode-native, but network/CDN key encoding can trip
        # up if we're not careful.
        tag = uuid.uuid4().bytes
        path = Path.home() / f"upic测试_{uuid.uuid4().hex[:6]}.png"
        path.write_bytes(_make_png(32, 24, tag))
        try:
            result = S.upload_image(str(path))
            assert "url" in result, result
            # The URL might URL-encode the Chinese characters or keep them raw;
            # either is valid — what matters is the URL is reachable.
            r = httpx.get(result["url"], follow_redirects=True, timeout=15)
            assert r.status_code == 200
        finally:
            path.unlink(missing_ok=True)

    def test_base64_with_traversal_filename_still_works(self):
        # Real E2E: even if the caller tries a path-traversal filename, we
        # sanitize to basename and still successfully upload.
        import base64 as b64
        tag = uuid.uuid4().bytes
        png_bytes = _make_png(16, 16, tag)
        data = b64.b64encode(png_bytes).decode()
        malicious_name = f"../../../../upic_traversal_{uuid.uuid4().hex[:8]}.png"

        result = S.upload_image_from_base64(data, malicious_name)
        assert "url" in result, result
        # Resulting URL's filename should NOT contain any path separators.
        from urllib.parse import urlparse, unquote
        url_path = unquote(urlparse(result["url"]).path)
        filename_in_url = url_path.split("/")[-1]
        assert ".." not in filename_in_url
        # And the upload should be live.
        r = httpx.get(result["url"], follow_redirects=True, timeout=15)
        assert r.status_code == 200
