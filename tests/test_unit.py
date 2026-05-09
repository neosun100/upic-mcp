"""Unit tests for pure helper functions in server.py.

These tests use no subprocess, no filesystem mutation, no network.
They should run in well under 1 second total.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `server` importable when running `pytest` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as S  # noqa: E402


# --- _extract_url ------------------------------------------------------------


class TestExtractURL:
    def test_normal_upload_stdout(self):
        stdout = (
            "共 1 个文件路径和链接\n"
            "Uploading ...\n"
            "Uploading 1/1\n"
            "Output URL:\n"
            "https://img.aws.xin/uPic/foo.png\n"
        )
        assert S._extract_url(stdout) == "https://img.aws.xin/uPic/foo.png"

    def test_http_url(self):
        stdout = "Output URL:\nhttp://example.com/bar.jpg\n"
        assert S._extract_url(stdout) == "http://example.com/bar.jpg"

    def test_no_output_url_marker_returns_none(self):
        stdout = "共 1 个文件路径和链接\nUploading ...\n"
        assert S._extract_url(stdout) is None

    def test_empty_stdout_returns_none(self):
        assert S._extract_url("") is None

    def test_url_with_trailing_whitespace_is_stripped(self):
        stdout = "Output URL:\n  https://img.aws.xin/uPic/x.png   \n"
        assert S._extract_url(stdout) == "https://img.aws.xin/uPic/x.png"

    def test_multiple_blank_lines_between_marker_and_url(self):
        # uPic sometimes emits a blank line right after "Output URL:" before the URL itself.
        stdout = "Output URL:\n\n\nhttps://img.aws.xin/uPic/y.png\n"
        assert S._extract_url(stdout) == "https://img.aws.xin/uPic/y.png"

    def test_non_url_after_marker_is_returned_as_error(self):
        # If uPic's slient mode is off and upload fails, the "URL" line contains an error message.
        stdout = "Output URL:\nInvalid file path\n"
        assert S._extract_url(stdout) == "Invalid file path"

    def test_multiple_urls_returns_first(self):
        stdout = (
            "Output URL:\n"
            "https://img.aws.xin/uPic/a.png\n"
            "https://img.aws.xin/uPic/b.png\n"
        )
        assert S._extract_url(stdout) == "https://img.aws.xin/uPic/a.png"


# --- _parse_hosts ------------------------------------------------------------


class TestParseHosts:
    def test_three_hosts_parsed_and_sanitized(self):
        defaults = {
            "uPic_hostItems": [
                '{"id":"1","name":"A","type":"s3","data":"{\\"secretKey\\":\\"SECRET\\"}"}',
                '{"id":"2","name":"B","type":"imgur"}',
                '{"id":"3","name":"C","type":"qiniu_kodo"}',
            ]
        }
        hosts = S._parse_hosts(defaults)
        assert len(hosts) == 3
        assert hosts[0] == {"id": "1", "name": "A", "type": "s3"}
        assert hosts[1] == {"id": "2", "name": "B", "type": "imgur"}
        # No secrets should leak through.
        for h in hosts:
            assert "data" not in h
            assert "secretKey" not in str(h)

    def test_empty_list(self):
        assert S._parse_hosts({"uPic_hostItems": []}) == []

    def test_missing_key(self):
        assert S._parse_hosts({}) == []

    def test_malformed_json_is_skipped(self):
        defaults = {
            "uPic_hostItems": [
                "{this is not json",
                '{"id":"ok","name":"ok","type":"s3"}',
            ]
        }
        hosts = S._parse_hosts(defaults)
        assert len(hosts) == 1
        assert hosts[0]["id"] == "ok"

    def test_non_string_entries_are_skipped(self):
        defaults = {
            "uPic_hostItems": [
                123,
                None,
                '{"id":"valid","name":"v","type":"s3"}',
            ]
        }
        hosts = S._parse_hosts(defaults)
        assert len(hosts) == 1

    def test_missing_fields_default_to_empty_strings(self):
        defaults = {"uPic_hostItems": ['{"id":"x"}']}
        hosts = S._parse_hosts(defaults)
        assert hosts == [{"id": "x", "name": "", "type": ""}]


# --- _needs_staging ----------------------------------------------------------


class TestNeedsStaging:
    def test_home_directory_does_not_need_staging(self, tmp_path, monkeypatch):
        # Create a real file inside $HOME so resolve() stays on a whitelisted prefix.
        p = Path.home() / "_upic_unit_test_needs_staging.txt"
        p.write_text("x")
        try:
            assert S._needs_staging(p) is False
        finally:
            p.unlink(missing_ok=True)

    def test_tmp_directory_needs_staging(self):
        # /tmp is a symlink to /private/tmp on macOS; resolved path is NOT in whitelist.
        p = Path("/tmp") / "_upic_unit_test_needs_staging.txt"
        p.write_text("x")
        try:
            assert S._needs_staging(p) is True
        finally:
            p.unlink(missing_ok=True)

    def test_private_var_needs_staging(self, tmp_path):
        # pytest's tmp_path lives under /private/var/folders/...
        assert S._needs_staging(tmp_path) is True

    def test_applications_subpath_does_not_need_staging(self):
        # /Applications/ prefix is on the whitelist; app bundles under it should upload directly.
        assert S._needs_staging(Path("/Applications/uPic.app/Contents/Info.plist")) is False

    def test_volumes_subpath_does_not_need_staging(self):
        # Files on mounted volumes (external drives etc.) should upload directly.
        assert S._needs_staging(Path("/Volumes/SomeDrive/photo.png")) is False


# --- _result_to_dict ---------------------------------------------------------


class TestResultToDict:
    def test_serializes_all_fields(self):
        r = S.UploadResult(
            url="https://img.aws.xin/uPic/foo.png",
            source="/Users/x/foo.png",
            staged_from=None,
            size_bytes=1234,
            host_name="Amazon S3",
            compress_factor=90,
            elapsed_ms=2100,
        )
        d = S._result_to_dict(r)
        assert d["url"] == "https://img.aws.xin/uPic/foo.png"
        assert d["size_bytes"] == 1234
        assert d["host"] == "Amazon S3"
        assert d["compress_factor"] == 90
        assert d["compression_enabled"] is True
        assert d["staged_from"] is None
        assert d["elapsed_ms"] == 2100

    def test_compression_disabled_when_factor_is_100(self):
        r = S.UploadResult(
            url="https://x/y", source="/a", staged_from=None, size_bytes=1,
            host_name="H", compress_factor=100, elapsed_ms=1,
        )
        assert S._result_to_dict(r)["compression_enabled"] is False
