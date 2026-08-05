"""Unit tests for desktop-only argument and path helpers."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desktop import launcher
from desktop import main as desktop_main
from desktop.export_api import DesktopExportApi
from desktop.main import _worker_args_from_process_argv


class DesktopBootstrapTests(unittest.TestCase):
    def test_legacy_frozen_worker_arguments(self):
        self.assertEqual(
            _worker_args_from_process_argv(["-u", "extract.py", "--incremental"]),
            ["extract", "--incremental"],
        )

    def test_explicit_worker_arguments(self):
        self.assertEqual(
            _worker_args_from_process_argv(["--worker", "extract", "--list-conversations"]),
            ["extract", "--list-conversations"],
        )

    def test_normal_gui_arguments_are_not_workers(self):
        self.assertIsNone(_worker_args_from_process_argv([]))
        self.assertIsNone(_worker_args_from_process_argv(["--unknown"]))

    def test_application_dir_uses_local_app_data(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"LOCALAPPDATA": tmp}, clear=False
        ):
            self.assertEqual(
                launcher.application_dir(),
                (Path(tmp) / launcher.APP_NAME).resolve(),
            )

    def test_run_desktop_passes_js_api_and_binds_window(self):
        class ClosedEvents:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        class FakeWindow:
            def __init__(self):
                self.events = SimpleNamespace(closed=ClosedEvents())

        class FakeServer:
            base_url = "http://127.0.0.1:1234"

            def start(self):
                self.started = True

            def stop(self, *_args):
                self.stopped = True

        server = FakeServer()
        window = FakeWindow()
        fake_webview = SimpleNamespace(__version__="test")

        def fake_create_window(*args, **kwargs):
            fake_webview.created_kwargs = kwargs
            return window

        fake_webview.create_window = fake_create_window
        fake_webview.start = lambda *args, **kwargs: None

        with (
            patch.object(desktop_main, "DesktopServer", return_value=server),
            patch("extractor.models.init_db") as init_db,
            patch.dict(sys.modules, {"webview": fake_webview}),
        ):
            result = desktop_main.run_desktop()

        self.assertEqual(result, 0)
        init_db.assert_called_once()
        self.assertTrue(server.started)
        self.assertTrue(server.stopped)
        self.assertIn("js_api", fake_webview.created_kwargs)
        self.assertIsInstance(
            fake_webview.created_kwargs["js_api"],
            DesktopExportApi,
        )
        bound_api = fake_webview.created_kwargs["js_api"]
        self.assertIs(bound_api._window, window)
        self.assertIn(server.stop, window.events.closed.handlers)


if __name__ == "__main__":
    unittest.main()
