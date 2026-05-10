"""Integration tests for the MCP tools in server.py.

These tests exercise the full tool code path (upload_image, list_hosts, ...)
but stub out the two real external dependencies:

  * ``_run_upic``     — the uPic CLI subprocess call
  * ``_read_upic_defaults`` — the UserDefaults plist read

That gives us deterministic behavior regardless of what's installed on the
machine, so these tests are safe to run in CI and don't actually hit the
network or the disk-resident uPic app.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as S  # noqa: E402


# --- fixtures ----------------------------------------------------------------


SAMPLE_DEFAULTS = {
    "uPic_DefaultHostId": 1775400305,
    "uPic_CompressFactor": 90,
    "uPic_hostItems": [
        '{"id":"1775400130","name":"\u4e03\u725b\u4e91 KODO","type":"qiniu_kodo",'
        '"data":"{\\"accessKey\\":\\"SECRET-AK\\",\\"secretKey\\":\\"SECRET-SK\\"}"}',
        '{"id":"1775400294","name":"Imgur","type":"imgur",'
        '"data":"{\\"clientId\\":\\"abc\\"}"}',
        '{"id":"1775400305","name":"Amazon S3","type":"s3",'
        '"data":"{\\"accessKey\\":\\"AK\\",\\"secretKey\\":\\"SK\\"}"}',
    ],
}


def _fake_run_upic_success(paths):
    """Fake uPic CLI output that returns a plausible URL for the given path."""
    name = Path(paths[0]).name
    stdout = (
        f"共 {len(paths)} 个文件路径和链接\n"
        "Uploading ...\n"
        "Uploading 1/1\n"
        "Output URL:\n"
        f"https://img.aws.xin/uPic/{name}\n"
    )
    return stdout, ""


def _fake_run_upic_failure(paths):
    stdout = "共 1 个文件路径和链接\nUploading ...\nOutput URL:\n"
    return stdout, "some error"


@pytest.fixture
def defaults_patch():
    """Patch UserDefaults reader to return our sample data."""
    with patch.object(S, "_read_upic_defaults", return_value=SAMPLE_DEFAULTS):
        yield


# --- upload_image ------------------------------------------------------------


class TestUploadImage:
    def test_upload_file_under_home_no_staging(self, tmp_path, defaults_patch, monkeypatch):
        # Put the file directly under $HOME so staging should NOT be triggered.
        home_file = Path.home() / "_upic_it_test_upload.png"
        home_file.write_bytes(b"PNGDATA" * 10)
        try:
            with patch.object(S, "_run_upic", side_effect=_fake_run_upic_success) as mock_cli:
                out = S.upload_image(str(home_file))

            assert out["url"] == f"https://img.aws.xin/uPic/{home_file.name}"
            assert out["staged_from"] is None
            assert out["size_bytes"] == home_file.stat().st_size
            assert out["host"] == "Amazon S3"
            assert out["compress_factor"] == 90
            assert out["compression_enabled"] is True
            assert "elapsed_ms" in out
            mock_cli.assert_called_once()
            # CLI was called with the file's real path (not a staged copy).
            (called_paths,) = mock_cli.call_args.args
            assert called_paths == [str(home_file)]
        finally:
            home_file.unlink(missing_ok=True)

    def test_upload_file_in_tmp_triggers_staging(self, tmp_path, defaults_patch):
        # tmp_path is under /private/var/folders/... — outside the sandbox whitelist.
        src = tmp_path / "needs_staging.png"
        src.write_bytes(b"PNGDATA-STAGING" * 100)

        with patch.object(S, "_run_upic", side_effect=_fake_run_upic_success) as mock_cli:
            out = S.upload_image(str(src))

        assert out["staged_from"] == str(src)
        # CLI was invoked with the staged copy, not the original path.
        (called_paths,) = mock_cli.call_args.args
        staged = Path(called_paths[0])
        assert staged.parent == S.STAGING_DIR
        assert staged.exists(), "staged file should exist on disk after staging"
        assert staged.read_bytes() == src.read_bytes()
        # Clean up what we staged so we don't leak between tests.
        staged.unlink(missing_ok=True)

    def test_missing_file_returns_error_without_invoking_cli(self, defaults_patch):
        with patch.object(S, "_run_upic") as mock_cli:
            out = S.upload_image("/nonexistent/path/xyz.png")
        assert "error" in out
        assert "not found" in out["error"]
        mock_cli.assert_not_called()

    def test_cli_failure_surfaces_as_runtime_error(self, defaults_patch):
        home_file = Path.home() / "_upic_it_test_failure.png"
        home_file.write_bytes(b"x")
        try:
            with patch.object(S, "_run_upic", side_effect=_fake_run_upic_failure):
                with pytest.raises(RuntimeError) as exc_info:
                    S.upload_image(str(home_file))
            # Error payload should include stdout+stderr for debuggability.
            assert "uPic upload failed" in str(exc_info.value)
        finally:
            home_file.unlink(missing_ok=True)

    def test_tilde_path_is_expanded(self, defaults_patch):
        # Create file under home, then pass the ~-prefixed path in.
        home_file = Path.home() / "_upic_it_test_tilde.png"
        home_file.write_bytes(b"data")
        try:
            with patch.object(S, "_run_upic", side_effect=_fake_run_upic_success):
                out = S.upload_image(f"~/{home_file.name}")
            assert "url" in out
        finally:
            home_file.unlink(missing_ok=True)

    def test_staging_deduplicates_by_content_hash(self, tmp_path, defaults_patch):
        src = tmp_path / "dup.png"
        src.write_bytes(b"SAME-CONTENT")

        with patch.object(S, "_run_upic", side_effect=_fake_run_upic_success) as mock_cli:
            S.upload_image(str(src))
            first_staged = Path(mock_cli.call_args.args[0][0])
            first_mtime = first_staged.stat().st_mtime
            # Upload the same content again; should reuse existing staged file.
            S.upload_image(str(src))
            second_staged = Path(mock_cli.call_args.args[0][0])
            second_mtime = second_staged.stat().st_mtime
        assert first_staged == second_staged
        assert first_mtime == second_mtime, (
            "staging should not overwrite identical content"
        )
        first_staged.unlink(missing_ok=True)


# --- upload_image_from_base64 -----------------------------------------------


class TestUploadImageFromBase64:
    def test_plain_base64(self, defaults_patch):
        data = base64.b64encode(b"PNG-BYTES").decode()
        with patch.object(S, "_run_upic", side_effect=_fake_run_upic_success) as mock_cli:
            out = S.upload_image_from_base64(data, "snap.png")
        assert "url" in out and out["url"].endswith("snap.png")
        # A staged file was created with the decoded bytes.
        staged = Path(mock_cli.call_args.args[0][0])
        assert staged.read_bytes() == b"PNG-BYTES"
        staged.unlink(missing_ok=True)

    def test_data_url_prefix_is_stripped(self, defaults_patch):
        raw = base64.b64encode(b"hello").decode()
        data_url = f"data:image/png;base64,{raw}"
        with patch.object(S, "_run_upic", side_effect=_fake_run_upic_success) as mock_cli:
            out = S.upload_image_from_base64(data_url, "x.png")
        assert "url" in out
        staged = Path(mock_cli.call_args.args[0][0])
        assert staged.read_bytes() == b"hello"
        staged.unlink(missing_ok=True)

    def test_invalid_base64_returns_error(self, defaults_patch):
        # Feed a string that is not valid base64 under any tolerance. Python's
        # b64decode will fail when it tries to interpret non-ASCII as the
        # base64 alphabet.
        with patch.object(S, "_run_upic") as mock_cli:
            out = S.upload_image_from_base64("不是合法的base64字符串！@#$%", "x.png")
        assert "error" in out, f"expected error payload, got: {out}"
        assert "base64" in out["error"].lower() or "empty" in out["error"].lower()
        mock_cli.assert_not_called(), "CLI must not run when decode fails"

    def test_empty_payload_returns_error(self, defaults_patch):
        out = S.upload_image_from_base64("", "x.png")
        assert "error" in out

    def test_default_filename_is_image_png(self, defaults_patch):
        data = base64.b64encode(b"x").decode()
        with patch.object(S, "_run_upic", side_effect=_fake_run_upic_success) as mock_cli:
            out = S.upload_image_from_base64(data)  # no filename arg
        assert "url" in out and out["url"].endswith("image.png")
        staged = Path(mock_cli.call_args.args[0][0])
        staged.unlink(missing_ok=True)


# --- list_hosts / get_default_host / uploader_info ---------------------------


class TestListHosts:
    def test_returns_all_hosts_with_is_default_flag(self, defaults_patch):
        result = S.list_hosts()
        assert result["count"] == 3
        names = {h["name"] for h in result["hosts"]}
        assert "Amazon S3" in names
        assert "Imgur" in names
        defaults = [h for h in result["hosts"] if h["is_default"]]
        assert len(defaults) == 1
        assert defaults[0]["id"] == "1775400305"

    def test_no_secrets_in_output(self, defaults_patch):
        result = S.list_hosts()
        blob = str(result)
        for secret in ("SECRET-AK", "SECRET-SK", "AK", "SK"):
            if secret in ("AK", "SK"):
                # single/double-letter codes are too short to check meaningfully;
                # they would match "Amazon S3" etc. Just check the obvious ones.
                continue
            assert secret not in blob


class TestGetDefaultHost:
    def test_returns_current_default(self, defaults_patch):
        host = S.get_default_host()
        assert host["id"] == "1775400305"
        assert host["name"] == "Amazon S3"
        assert host["type"] == "s3"

    def test_returns_error_when_no_default_configured(self):
        with patch.object(S, "_read_upic_defaults", return_value={}):
            result = S.get_default_host()
        assert "error" in result


class TestUploaderInfo:
    def test_returns_runtime_info(self, defaults_patch):
        info = S.uploader_info()
        assert info["upic_binary"] == "/Applications/uPic.app/Contents/MacOS/uPic"
        assert info["staging_dir"].endswith(".upic-staging")
        assert info["compress_factor"] == 90
        assert info["compression_enabled"] is True
        assert info["default_host"]["name"] == "Amazon S3"

    def test_detects_missing_binary(self, defaults_patch, monkeypatch):
        monkeypatch.setattr(S, "UPIC_BINARY", "/nonexistent/uPic")
        info = S.uploader_info()
        assert info["upic_binary_exists"] is False


# --- list_hosts edge cases ---------------------------------------------------


class TestListHostsEdgeCases:
    def test_no_default_host_set(self):
        # Empty defaults — no hosts, no default ID.
        with patch.object(S, "_read_upic_defaults", return_value={}):
            result = S.list_hosts()
        assert result["count"] == 0
        assert result["hosts"] == []

    def test_hosts_configured_but_no_default(self):
        # Edge case: hosts exist but no `uPic_DefaultHostId` key.
        defaults = {
            "uPic_hostItems": ['{"id":"x","name":"X","type":"s3"}'],
        }
        with patch.object(S, "_read_upic_defaults", return_value=defaults):
            result = S.list_hosts()
        assert result["count"] == 1
        # No host should be marked as default.
        assert all(h["is_default"] is False for h in result["hosts"])


# --- subprocess timeout / failure -------------------------------------------


class TestSubprocessFailures:
    """Verify we degrade gracefully when the uPic subprocess misbehaves."""

    def test_timeout_raises_rather_than_hangs(self, defaults_patch):
        # Simulate a hung uPic: _run_upic raises TimeoutExpired.
        import subprocess as sp
        home_file = Path.home() / "_upic_it_test_timeout.png"
        home_file.write_bytes(b"x")
        try:
            with patch.object(
                S, "_run_upic",
                side_effect=sp.TimeoutExpired(cmd=[S.UPIC_BINARY], timeout=120),
            ):
                with pytest.raises(sp.TimeoutExpired):
                    S.upload_image(str(home_file))
        finally:
            home_file.unlink(missing_ok=True)

    def test_base64_upload_cleans_up_staging_on_failure(self, tmp_path, monkeypatch, defaults_patch):
        # When uPic fails, the staging file this tool created should be cleaned
        # up rather than orphaned in ~/.upic-staging/.
        import base64 as b64
        monkeypatch.setattr(S, "STAGING_DIR", tmp_path)

        data = b64.b64encode(b"CLEANUP-TEST-BYTES").decode()
        # Stub _run_upic to simulate an upload failure that reaches _upload_path.
        def _fake_failure(paths):
            return "Output URL:\nInvalid file path\n", ""

        with patch.object(S, "_run_upic", side_effect=_fake_failure):
            with pytest.raises(RuntimeError):
                S.upload_image_from_base64(data, "cleanup.png")

        # After failure, staging directory should be empty.
        remaining = list(tmp_path.iterdir())
        assert remaining == [], f"expected staging cleanup, found: {remaining}"

    def test_path_upload_does_NOT_delete_user_file_on_failure(self, defaults_patch):
        # If the user passes a real file path and upload fails, we must NOT
        # delete their file (it's their data, not our staging).
        home_file = Path.home() / "_upic_it_test_nodelete.png"
        home_file.write_bytes(b"USER-DATA")
        try:
            def _fake_failure(paths):
                return "Output URL:\nError\n", ""
            with patch.object(S, "_run_upic", side_effect=_fake_failure):
                with pytest.raises(RuntimeError):
                    S.upload_image(str(home_file))
            # User's file must still exist and be unchanged.
            assert home_file.exists()
            assert home_file.read_bytes() == b"USER-DATA"
        finally:
            home_file.unlink(missing_ok=True)
