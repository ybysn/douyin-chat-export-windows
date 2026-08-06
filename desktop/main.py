"""Executable entry point for the Windows desktop build."""
from __future__ import annotations

import asyncio
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
from pathlib import Path
import platform
import sys

from desktop.launcher import (
    APP_TITLE,
    DesktopServer,
    application_dir,
    configure_environment,
    is_frozen,
    resource_dir,
)
from desktop.export_api import DesktopExportApi

_LOG_FILE_NAME = "desktop.log"


def configure_startup_logging() -> Path:
    """Create a persistent log before importing the application backend."""
    log_dir = application_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / _LOG_FILE_NAME

    handler = RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"
        )
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    logging.captureWarnings(True)
    return log_path


def log_startup_context(log_path: Path) -> None:
    logging.info("Starting %s", APP_TITLE)
    logging.info("Python: %s", sys.version.replace("\n", " "))
    logging.info("Platform: %s", platform.platform())
    logging.info("Executable: %s", sys.executable)
    logging.info("Frozen: %s", is_frozen())
    logging.info("Working directory before configuration: %s", os.getcwd())
    logging.info("Resource directory: %s", resource_dir())
    logging.info("Application directory: %s", application_dir())
    logging.info("Log file: %s", log_path)


def show_fatal_error(error: BaseException, log_path: Path | None) -> None:
    details = f"{type(error).__name__}: {error}"
    log_text = str(log_path) if log_path is not None else "日志文件未能创建"
    message = (
        "应用启动失败。\n\n"
        f"{details}\n\n"
        f"诊断日志：\n{log_text}"
    )

    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                message,
                f"{APP_TITLE} - 启动失败",
                0x10,
            )
            return
        except Exception:
            pass

    print(message, file=sys.stderr)


def run_worker(argv: list[str]) -> int:
    if not argv or argv[0] != "extract":
        print("[-] 未知 worker 类型", file=sys.stderr)
        return 2

    # Recreate the source CLI contract expected by extract._parse_args().
    sys.argv = ["extract.py", *argv[1:]]
    import extract

    return int(asyncio.run(extract.run()) or 0)


def _worker_args_from_process_argv(argv: list[str]) -> list[str] | None:
    if len(argv) >= 2 and argv[0] == "--worker":
        return argv[1:]

    # Existing control-panel code launches:
    #   [sys.executable, "-u", "extract.py", ...]
    # In a PyInstaller build sys.executable is this GUI executable, so accept
    # the legacy Python command shape and route it to the bundled worker.
    if len(argv) >= 2 and argv[0] == "-u" and argv[1].endswith("extract.py"):
        return ["extract", *argv[2:]]
    if argv and argv[0].endswith("extract.py"):
        return ["extract", *argv[1:]]
    return None


def run_desktop() -> int:
    logging.info("Stage: importing database model")
    from extractor.models import init_db

    logging.info("Stage: initializing database")
    init_db()

    logging.info("Stage: creating local server")
    server = DesktopServer()

    try:
        logging.info("Stage: starting local server at %s", server.base_url)
        server.start()
        logging.info("Stage: local server is ready")

        logging.info("Stage: creating desktop export API")
        desktop_export_api = DesktopExportApi()

        logging.info("Stage: importing pywebview")
        import webview

        logging.info(
            "PyWebView module loaded; version=%s",
            getattr(webview, "__version__", "unknown"),
        )

        logging.info("Stage: creating desktop window")
        window = webview.create_window(
            APP_TITLE,
            f"{server.base_url}/panel",
            js_api=desktop_export_api,
            width=1280,
            height=820,
            min_size=(980, 680),
            text_select=True,
        )
        desktop_export_api.bind_window(window)
        window.events.closed += server.stop

        logging.info("Stage: entering PyWebView event loop")
        webview.start(debug=False)
        logging.info("PyWebView event loop exited normally")
        return 0
    finally:
        logging.info("Stopping local server")
        server.stop()


def main() -> int:
    # Mark this process as the desktop build BEFORE configure_environment()
    # imports anything that resolves paths (common.paths, backend.control_panel,
    # extractor.web_scraper). Workers inherit the flag from this environment.
    os.environ.setdefault("DOUYIN_CHAT_DESKTOP", "1")
    resources, app_dir = configure_environment()
    logging.info("Environment configured")
    logging.info("Configured resource directory: %s", resources)
    logging.info("Configured application directory: %s", app_dir)
    logging.info("Configured data directory: %s", os.environ.get("DOUYIN_CHAT_DATA_DIR"))

    worker_args = _worker_args_from_process_argv(sys.argv[1:])
    if worker_args is not None:
        logging.info("Running bundled worker")
        return run_worker(worker_args)

    return run_desktop()


def run_entrypoint() -> int:
    log_path: Path | None = None
    try:
        log_path = configure_startup_logging()
        log_startup_context(log_path)
        return main()
    except Exception as error:
        try:
            logging.exception("Fatal application startup error")
        finally:
            if _worker_args_from_process_argv(sys.argv[1:]) is None:
                show_fatal_error(error, log_path)
        return 1
    finally:
        logging.shutdown()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(run_entrypoint())
