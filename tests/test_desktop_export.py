"""Tests for the desktop export save API and backend export-state safety."""

import json
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import backend.control_panel as control_panel
import backend.main as main
import common.paths as paths
from desktop.export_api import DesktopExportApi


class _FakeWebView:
    SAVE_DIALOG = "save"


@pytest.fixture(autouse=True)
def _reset_export_state():
    saved = dict(control_panel._export_state)
    yield
    control_panel._export_state.clear()
    control_panel._export_state.update(saved)


@pytest.fixture
def data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    return data


@pytest.fixture
def export_api(data_dir):
    return DesktopExportApi(data_dir=data_dir)


def _make_window(selected):
    return SimpleNamespace(create_file_dialog=lambda *args, **kwargs: selected)


def _write_source(api, name, content=b"{}"):
    source = api._data_dir / name
    source.write_bytes(content)
    return source


# ── DesktopExportApi behavior ──


@pytest.mark.parametrize(
    "name,content",
    [
        ("export.json", b'{"ok": true}'),
        ("export.jsonl", '{"ok": true}\n'.encode("utf-8")),
        ("export.zip", b"PK\x03\x04fakezip"),
    ],
)
def test_save_success(tmp_path, export_api, name, content):
    source = _write_source(export_api, name, content)
    target = tmp_path / "chosen" / name
    target.parent.mkdir()
    export_api.bind_window(_make_window(str(target)))

    with patch.dict(sys.modules, {"webview": _FakeWebView}):
        result = export_api.save_export(name)

    assert result == {
        "status": "saved",
        "path": str(target),
        "message": "文件已保存",
    }
    assert target.read_bytes() == content
    assert source.read_bytes() == content


@pytest.mark.parametrize("selected", [None, ()])
def test_save_cancelled(export_api, selected):
    _write_source(export_api, "export.json")
    export_api.bind_window(_make_window(selected))

    with patch.dict(sys.modules, {"webview": _FakeWebView}):
        result = export_api.save_export("export.json")

    assert result == {"status": "cancelled", "message": "已取消保存"}


def test_source_missing(export_api):
    result = export_api.save_export("missing.json")
    assert result["status"] == "error"
    assert "不存在" in result["message"]
    assert "Traceback" not in result["message"]


@pytest.mark.parametrize("name", ["notes.txt", "export.csv", "archive.tar.gz"])
def test_unsupported_extension(export_api, name):
    result = export_api.save_export(name)
    assert result["status"] == "error"
    assert "仅支持" in result["message"]
    assert "Traceback" not in result["message"]


@pytest.mark.parametrize("name", ["../evil.json", "..\\evil.json", "a/../evil.json"])
def test_path_traversal_rejected(export_api, name):
    result = export_api.save_export(name)
    assert result["status"] == "error"
    assert "文件名无效" in result["message"]
    assert "Traceback" not in result["message"]


@pytest.mark.parametrize(
    "name",
    ["C:\\Windows\\evil.json", "/etc/evil.json", "\\\\server\\share\\evil.json"],
)
def test_absolute_path_rejected(export_api, name):
    result = export_api.save_export(name)
    assert result["status"] == "error"
    assert "文件名无效" in result["message"]
    assert "Traceback" not in result["message"]


def test_source_outside_data_dir_rejected(tmp_path, export_api):
    outside = tmp_path / "outside.json"
    outside.write_text("secret", encoding="utf-8")
    link = export_api._data_dir / "link.json"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"symlinks not available: {exc}")

    result = export_api.save_export("link.json")
    assert result["status"] == "error"
    assert "数据目录" in result["message"]
    assert "Traceback" not in result["message"]


def test_overwrites_existing_target(tmp_path, export_api):
    _write_source(export_api, "export.json", b"new-content")
    target = tmp_path / "existing" / "export.json"
    target.parent.mkdir()
    target.write_bytes(b"old-content")
    export_api.bind_window(_make_window(str(target)))

    with patch.dict(sys.modules, {"webview": _FakeWebView}):
        result = export_api.save_export("export.json")

    assert result["status"] == "saved"
    assert target.read_bytes() == b"new-content"


def test_copy_failure_returns_readable_error(tmp_path, export_api):
    _write_source(export_api, "export.json")
    export_api.bind_window(_make_window(str(tmp_path / "target.json")))

    with (
        patch("desktop.export_api.shutil.copy2", side_effect=OSError("disk full")),
        patch.dict(sys.modules, {"webview": _FakeWebView}),
    ):
        result = export_api.save_export("export.json")

    assert result["status"] == "error"
    assert result["message"].startswith("保存失败")
    assert "Traceback" not in result["message"]


def test_dialog_failure_returns_readable_error(export_api):
    _write_source(export_api, "export.json")

    def boom(*args, **kwargs):
        raise RuntimeError("dialog exploded")

    export_api.bind_window(SimpleNamespace(create_file_dialog=boom))
    with patch.dict(sys.modules, {"webview": _FakeWebView}):
        result = export_api.save_export("export.json")

    assert result["status"] == "error"
    assert result["message"].startswith("打开保存对话框失败")
    assert "Traceback" not in result["message"]


def test_window_not_bound_returns_clear_error(export_api):
    _write_source(export_api, "export.json")
    result = export_api.save_export("export.json")
    assert result["status"] == "error"
    assert "窗口" in result["message"]
    assert "Traceback" not in result["message"]


def test_save_dialog_empty_list_cancelled(export_api):
    _write_source(export_api, "export.json")
    export_api.bind_window(_make_window([]))

    with patch.dict(sys.modules, {"webview": _FakeWebView}):
        result = export_api.save_export("export.json")

    assert result == {"status": "cancelled", "message": "已取消保存"}


def test_save_dialog_single_element_tuple(tmp_path, export_api):
    source = _write_source(export_api, "export.json")
    target = tmp_path / "single.json"
    export_api.bind_window(_make_window((str(target),)))

    with patch.dict(sys.modules, {"webview": _FakeWebView}):
        result = export_api.save_export("export.json")

    assert result["status"] == "saved"
    assert result["path"] == str(target)
    assert target.read_bytes() == source.read_bytes()


def test_save_dialog_string(tmp_path, export_api):
    _write_source(export_api, "export.zip", b"PK\x03\x04")
    target = tmp_path / "string-result.zip"
    export_api.bind_window(_make_window(str(target)))

    with patch.dict(sys.modules, {"webview": _FakeWebView}):
        result = export_api.save_export("export.zip")

    assert result["status"] == "saved"
    assert target.read_bytes() == b"PK\x03\x04"


def test_save_target_same_as_source(export_api):
    source = _write_source(export_api, "export.json", b"same")
    export_api.bind_window(_make_window(str(source)))
    copy2 = MagicMock()

    with (
        patch("desktop.export_api.shutil.copy2", copy2),
        patch.dict(sys.modules, {"webview": _FakeWebView}),
    ):
        result = export_api.save_export("export.json")

    assert result == {"status": "saved", "path": str(source), "message": "文件已保存"}
    copy2.assert_not_called()
    assert source.read_bytes() == b"same"


def test_save_target_without_extension_appends_source_suffix(tmp_path, export_api):
    _write_source(export_api, "export.json", b'{"ok": true}')
    target_no_ext = tmp_path / "导出 记录" / "聊天记录"
    target_no_ext.parent.mkdir()
    export_api.bind_window(_make_window(str(target_no_ext)))

    with patch.dict(sys.modules, {"webview": _FakeWebView}):
        result = export_api.save_export("export.json")

    assert result["status"] == "saved"
    expected = tmp_path / "导出 记录" / "聊天记录.json"
    assert result["path"] == str(expected)
    assert expected.read_bytes() == b'{"ok": true}'


def test_save_chinese_and_space_path(tmp_path, export_api):
    _write_source(export_api, "export.jsonl", '{"a":1}\n'.encode("utf-8"))
    target = tmp_path / "中文 目录" / "聊天记录 export.jsonl"
    target.parent.mkdir()
    export_api.bind_window(_make_window(str(target)))

    with patch.dict(sys.modules, {"webview": _FakeWebView}):
        result = export_api.save_export("export.jsonl")

    assert result["status"] == "saved"
    assert target.read_bytes() == '{"a":1}\n'.encode("utf-8")


def test_copy_permission_error_returns_readable_message(tmp_path, export_api):
    _write_source(export_api, "export.json")
    export_api.bind_window(_make_window(str(tmp_path / "target.json")))

    with (
        patch(
            "desktop.export_api.shutil.copy2",
            side_effect=PermissionError(13, "Permission denied"),
        ),
        patch.dict(sys.modules, {"webview": _FakeWebView}),
    ):
        result = export_api.save_export("export.json")

    assert result["status"] == "error"
    assert result["message"] == "保存失败：没有权限写入目标位置"
    assert "Traceback" not in result["message"]


def test_target_empty_string_tuple_rejected(export_api):
    _write_source(export_api, "export.json")
    export_api.bind_window(_make_window(("",)))
    copy2 = MagicMock()

    with (
        patch("desktop.export_api.shutil.copy2", copy2),
        patch.dict(sys.modules, {"webview": _FakeWebView}),
    ):
        result = export_api.save_export("export.json")

    assert result["status"] == "error"
    assert "保存路径无效" in result["message"]
    copy2.assert_not_called()


def test_target_directory_rejected(export_api, data_dir):
    _write_source(export_api, "export.json")
    export_api.bind_window(_make_window(str(data_dir)))
    copy2 = MagicMock()

    with (
        patch("desktop.export_api.shutil.copy2", copy2),
        patch.dict(sys.modules, {"webview": _FakeWebView}),
    ):
        result = export_api.save_export("export.json")

    assert result["status"] == "error"
    assert "目标位置无效" in result["message"]
    copy2.assert_not_called()


def test_error_results_never_contain_traceback(export_api):
    results = [
        export_api.save_export(""),
        export_api.save_export(None),
        export_api.save_export("../x.json"),
        export_api.save_export("missing.json"),
        export_api.save_export("bad.txt"),
    ]
    for result in results:
        assert result["status"] == "error"
        assert "Traceback" not in json.dumps(result, ensure_ascii=False)


# ── Web / Docker compatibility and backend export-state safety ──


@pytest.fixture
def client():
    return TestClient(main.app)


def test_web_download_route_still_present(client):
    response = client.get("/panel/api/export/download")
    assert response.status_code == 404


def test_panel_html_keeps_web_download_and_desktop_bridge():
    html = control_panel.PANEL_HTML
    assert 'href="/panel/api/export/download"' in html
    assert "window.pywebview.api.save_export" in html
    assert "typeof window.pywebview.api.save_export === 'function'" in html
    assert "saveExportSuccess:" in html
    assert "saveExportCancelled:" in html
    assert "saveExportFailed:" in html


def test_start_export_clears_stale_state(client, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "panel_config.json"))
    control_panel._export_state.update(
        {"status": "completed", "file_path": "export.json", "message": "old"}
    )
    observed = {}

    def fake_do_export(fmt, filter_name, conversations):
        observed.update(dict(control_panel._export_state))

    monkeypatch.setattr(control_panel, "_do_export", fake_do_export)
    response = client.post("/panel/api/export", json={"format": "jsonl"})

    assert response.status_code == 200
    assert observed["status"] == "running"
    assert observed["file_path"] is None
    assert "正在导出" in observed["message"]


def test_failed_export_clears_old_download(client, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "panel_config.json"))
    control_panel._export_state.update(
        {"status": "completed", "file_path": "export.json", "message": "old"}
    )

    def failing_do_export(fmt, filter_name, conversations):
        control_panel._export_state["status"] = "failed"
        control_panel._export_state["file_path"] = None
        control_panel._export_state["message"] = "导出失败: boom"

    monkeypatch.setattr(control_panel, "_do_export", failing_do_export)
    response = client.post("/panel/api/export", json={"format": "json"})

    assert response.status_code == 200
    assert control_panel._export_state["status"] == "failed"
    assert control_panel._export_state["file_path"] is None
    assert client.get("/panel/api/export/download").status_code == 404


def test_invalid_export_format_returns_400(client):
    control_panel._export_state.update(
        {"status": "idle", "file_path": None, "message": ""}
    )
    response = client.post("/panel/api/export", json={"format": "xml"})
    assert response.status_code == 400
    assert control_panel._export_state["status"] != "running"


def test_download_rejects_path_traversal(client, monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside = tmp_path / "secret.json"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    control_panel._export_state.update(
        {"status": "completed", "file_path": "../secret.json", "message": "ok"}
    )

    response = client.get("/panel/api/export/download")
    assert response.status_code == 404


def test_download_serves_valid_export_file(client, monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    export_file = data_dir / "export.json"
    export_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    control_panel._export_state.update(
        {"status": "completed", "file_path": "export.json", "message": "ok"}
    )

    response = client.get("/panel/api/export/download")
    assert response.status_code == 200
    assert response.content == b"{}"


def test_download_rejects_absolute_path(client, monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    control_panel._export_state.update(
        {"status": "completed", "file_path": str(tmp_path / "secret.json"), "message": "ok"}
    )

    assert client.get("/panel/api/export/download").status_code == 404


def test_download_rejects_subdirectory_path(client, monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    sub = data_dir / "sub"
    sub.mkdir(parents=True)
    (sub / "export.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    control_panel._export_state.update(
        {"status": "completed", "file_path": "sub/export.json", "message": "ok"}
    )

    assert client.get("/panel/api/export/download").status_code == 404


def test_download_rejects_unsupported_extension(client, monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "export.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    control_panel._export_state.update(
        {"status": "completed", "file_path": "export.txt", "message": "ok"}
    )

    assert client.get("/panel/api/export/download").status_code == 404


def test_concurrent_export_returns_409(client):
    control_panel._export_state.update(
        {"status": "running", "file_path": None, "message": "正在导出..."}
    )
    response = client.post("/panel/api/export", json={"format": "jsonl"})
    assert response.status_code == 409


def test_export_executor_failure_does_not_stick_running(client, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "panel_config.json"))

    def boom(fmt, filter_name, conversations):
        raise RuntimeError("executor boom")

    monkeypatch.setattr(control_panel, "_do_export", boom)
    response = client.post("/panel/api/export", json={"format": "json"})

    assert response.status_code == 200
    assert control_panel._export_state["status"] == "failed"
    assert control_panel._export_state["file_path"] is None


def test_panel_save_recovery_uses_finally_and_guards_repeat_click():
    html = control_panel.PANEL_HTML
    assert "btn.disabled = true;" in html
    assert "} finally {" in html
    assert "btn.disabled = false;" in html
    assert "if (btn.disabled) {" in html


def test_panel_web_mode_keeps_http_download_without_pywebview():
    html = control_panel.PANEL_HTML
    assert "if (!isDesktopSaveAvailable()) {" in html
    assert "return true; // plain browser/Docker flow keeps the normal download link" in html
    assert 'href="/panel/api/export/download"' in html


class _FakeChatLabExporter:
    def __init__(self, **kwargs):
        self.conv_name = kwargs.get("conv_name")

    def export(self, path):
        Path(path).write_text("{}", encoding="utf-8")
        return {"conv_id": self.conv_name or "7670159743629017637"}


def test_single_export_returns_id_date_file_name(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("extractor.exporter.ChatLabExporter", _FakeChatLabExporter)
    monkeypatch.setattr("extractor.exporter._export_date", lambda: "2026-08-09")

    control_panel._do_export("jsonl", "", None)

    assert control_panel._export_state["status"] == "completed"
    assert control_panel._export_state["file_path"] == "7670159743629017637_2026-08-09.jsonl"
    assert not Path(control_panel._export_state["file_path"]).is_absolute()
    assert (tmp_path / "data" / "7670159743629017637_2026-08-09.jsonl").is_file()


def test_single_export_sanitizes_windows_unsafe_id(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    monkeypatch.setattr("extractor.exporter.ChatLabExporter", _FakeChatLabExporter)
    monkeypatch.setattr("extractor.exporter._export_date", lambda: "2026-08-09")

    control_panel._do_export("jsonl", "0:1:user/a", ["0:1:user/a"])

    assert control_panel._export_state["status"] == "completed"
    assert control_panel._export_state["file_path"] == "0_1_user_a_2026-08-09.jsonl"
    assert (data_dir / "0_1_user_a_2026-08-09.jsonl").is_file()


def test_multi_export_returns_zip_file_name(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    monkeypatch.setattr("extractor.exporter.ChatLabExporter", _FakeChatLabExporter)
    monkeypatch.setattr("extractor.exporter._export_date", lambda: "2026-08-09")

    control_panel._do_export("jsonl", "", ["会话A", "会话B"])

    assert control_panel._export_state["status"] == "completed"
    assert control_panel._export_state["file_path"] == "export.zip"
    zip_path = data_dir / "export.zip"
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == [
            "会话A_2026-08-09.jsonl",
            "会话B_2026-08-09.jsonl",
        ]


def test_multi_export_avoids_colliding_arcnames(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    monkeypatch.setattr("extractor.exporter.ChatLabExporter", _FakeChatLabExporter)
    monkeypatch.setattr("extractor.exporter._export_date", lambda: "2026-08-09")

    control_panel._do_export("jsonl", "", ["A/B", "A\\B", "A B", "CON", "", ""])

    assert control_panel._export_state["status"] == "completed"
    zip_path = data_dir / "export.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert len(names) == len(set(names))
    assert "A_B_2026-08-09.jsonl" in names
    assert "A_B_2026-08-09_2.jsonl" in names
    assert "A_B_2026-08-09_3.jsonl" in names
    assert "_CON_2026-08-09.jsonl" in names
    assert "7670159743629017637_2026-08-09.jsonl" in names
    assert "7670159743629017637_2026-08-09_2.jsonl" in names


def test_multi_export_zip_entries_are_relative_basenames(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    monkeypatch.setattr("extractor.exporter.ChatLabExporter", _FakeChatLabExporter)
    monkeypatch.setattr("extractor.exporter._export_date", lambda: "2026-08-09")

    control_panel._do_export("json", "", ["会话A", "会话B"])

    zip_path = data_dir / "export.zip"
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            assert not Path(name).is_absolute()
            assert ".." not in name
            assert "/" not in name
            assert "\\" not in name
