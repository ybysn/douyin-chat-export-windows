"""Desktop export save API exposed to the PyWebView control panel.

The web panel normally downloads exports through
``GET /panel/api/export/download``. Plain HTTP downloads are not reliable in a
PyWebView window, so the Windows desktop build exposes this JS API instead.

The panel only ever passes the *file name* reported by the backend export
state. This class resolves that name strictly inside ``DOUYIN_CHAT_DATA_DIR``
and opens the Windows native Save As dialog through the bound PyWebView
window, then copies the export file to the user-selected location.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

_ALLOWED_SUFFIXES = (".json", ".jsonl", ".zip")

_FILE_FILTERS = {
    ".json": ("JSON 文件 (*.json)", "所有文件 (*.*)"),
    ".jsonl": ("JSONL 文件 (*.jsonl)", "所有文件 (*.*)"),
    ".zip": ("ZIP 压缩包 (*.zip)", "所有文件 (*.*)"),
}

_LOGGER = logging.getLogger(__name__)


class DesktopExportApi:
    """Single-purpose JS API for saving completed exports to a user-chosen path."""

    def __init__(self, data_dir: str | os.PathLike | None = None) -> None:
        # Resolve lazily: in frozen builds common.paths must not be imported
        # before configure_environment() sets DOUYIN_CHAT_DATA_DIR, otherwise
        # the whole app would resolve its data root inside the bundle.
        self._data_dir = Path(data_dir).resolve() if data_dir is not None else None
        self._window: Any | None = None

    def bind_window(self, window: Any) -> None:
        """Attach the PyWebView window used to open the native save dialog."""
        self._window = window

    def save_export(self, file_name: str) -> dict:
        """Save ``file_name`` from the data directory via a native Save As dialog.

        Returns a structured result: ``saved`` / ``cancelled`` / ``error``.
        Never raises and never returns a Python traceback.
        """
        try:
            source = self._resolve_source(file_name)
        except ValueError as exc:
            _LOGGER.warning("Rejected export save request: %s", exc)
            return {"status": "error", "message": str(exc)}

        window = self._window
        if window is None:
            message = "保存失败：桌面窗口未就绪，请重试"
            _LOGGER.error(message)
            return {"status": "error", "message": message}

        try:
            import webview
        except Exception as exc:  # desktop-only dependency
            message = f"保存失败：无法加载桌面保存组件（{exc}）"
            _LOGGER.exception("Failed to import pywebview for export save")
            return {"status": "error", "message": message}

        suffix = source.suffix.lower()
        try:
            selected = window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=source.name,
                file_types=_FILE_FILTERS[suffix],
            )
        except Exception as exc:
            _LOGGER.exception("Failed to open native save dialog")
            return {"status": "error", "message": "打开保存对话框失败，请重试"}

        # pywebview returns None when the dialog is cancelled; some platforms
        # return an empty tuple instead.
        if not selected:
            return {"status": "cancelled", "message": "已取消保存"}

        target = self._normalize_target(selected, source)
        if isinstance(target, dict):
            return target

        try:
            # Selecting the export file itself is a no-op, not an error.
            if target.resolve() == source.resolve():
                return {"status": "saved", "path": str(target), "message": "文件已保存"}
            shutil.copy2(source, target)
        except PermissionError:
            _LOGGER.exception("Permission denied while copying export file")
            return {"status": "error", "message": "保存失败：没有权限写入目标位置"}
        except OSError:
            _LOGGER.exception("OSError while copying export file")
            return {
                "status": "error",
                "message": "保存失败：无法复制文件，请检查磁盘空间或文件是否被占用",
            }
        except Exception:
            _LOGGER.exception("Failed to copy export file")
            return {"status": "error", "message": "保存失败：无法复制导出文件"}

        return {"status": "saved", "path": str(target), "message": "文件已保存"}

    def _normalize_target(self, selected: Any, source: Path) -> Path | dict:
        """Convert the dialog result into a safe target path, or an error dict."""
        if isinstance(selected, (tuple, list)):
            if not selected:
                return {"status": "cancelled", "message": "已取消保存"}
            raw = selected[0]
        else:
            raw = selected

        if not isinstance(raw, str) or not raw.strip():
            return {"status": "error", "message": "保存失败：保存路径无效"}

        target = Path(raw.strip())
        if target.exists() and target.is_dir():
            return {"status": "error", "message": "保存失败：目标位置无效，请选择文件路径"}

        # The native dialog usually appends the selected filter's extension, but
        # explicitly complete it when the user typed a name without one.
        if target.suffix.lower() not in _ALLOWED_SUFFIXES and source.suffix:
            target = Path(f"{target}{source.suffix}")

        if not str(target).strip() or target == Path("."):
            return {"status": "error", "message": "保存失败：保存路径无效"}
        return target

    def _resolve_source(self, file_name: str) -> Path:
        """Validate ``file_name`` and return the resolved source file path."""
        if not isinstance(file_name, str) or not file_name.strip():
            raise ValueError("保存失败：导出文件名无效")

        name = file_name.strip()
        candidate = Path(name)
        if (
            candidate.is_absolute()
            or candidate.name != name
            or "/" in name
            or "\\" in name
            or any(part == ".." for part in candidate.parts)
        ):
            raise ValueError("保存失败：导出文件名无效")

        suffix = candidate.suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise ValueError("保存失败：仅支持 .json、.jsonl 和 .zip 导出文件")

        data_dir = self._resolve_data_dir()
        source = (data_dir / name).resolve()
        if not source.is_relative_to(data_dir):
            raise ValueError("保存失败：导出文件不在数据目录内")
        if not source.is_file():
            raise ValueError("保存失败：导出文件不存在")
        return source

    def _resolve_data_dir(self) -> Path:
        if self._data_dir is None:
            from common import paths

            self._data_dir = Path(paths.DATA_DIR).resolve()
        return self._data_dir
