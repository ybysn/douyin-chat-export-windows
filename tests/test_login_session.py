"""Tests for unified login/session handling across the Windows desktop build.

Playwright is always mocked: no test may open a real browser or pop a real
login window.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import backend.control_panel as control_panel
import common.paths as paths
from extractor import web_scraper


class FakePage:
    def __init__(self):
        self.screenshots = 0
        self.goto_count = 0

    async def goto(self, *args, **kwargs):
        self.goto_count += 1

    async def screenshot(self, *args, **kwargs):
        self.screenshots += 1
        return b"png-data"

    async def wait_for_selector(self, *args, **kwargs):
        raise TimeoutError("no selector in fake")


class FakeContext:
    def __init__(self, cookies=None, events=None):
        self.pages = [FakePage()]
        self._cookies = list(cookies or [])
        self.closed = False
        self.close_handlers = []
        self.launch_kwargs = {}
        self.user_data_dir = None
        self.events = events if events is not None else []

    def on(self, event, handler):
        if event == "close":
            self.close_handlers.append(handler)

    def fire_close(self):
        for handler in list(self.close_handlers):
            handler()

    async def add_init_script(self, *args, **kwargs):
        pass

    async def cookies(self, url):
        return self._cookies

    async def add_cookies(self, cookies):
        self._cookies = list(cookies)

    async def new_page(self):
        return FakePage()

    async def close(self):
        self.closed = True
        self.events.append("ctx_closed")
        self.fire_close()


class FakeChromium:
    def __init__(self, cookies=None):
        self.cookies = list(cookies or [])
        self.launched = []
        self.events = []

    async def launch_persistent_context(self, user_data_dir, **kwargs):
        ctx = FakeContext(cookies=self.cookies, events=self.events)
        ctx.user_data_dir = user_data_dir
        ctx.launch_kwargs = kwargs
        self.launched.append(ctx)
        return ctx


class FakePW:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    async def stop(self):
        self.stopped = True
        self.chromium.events.append("pw_stopped")


class FakeAsyncPlaywright:
    def __init__(self, chromium=None):
        self.chromium = chromium or FakeChromium()

    async def start(self):
        return FakePW(self.chromium)


def _fake_async_playwright_module(chromium):
    return SimpleNamespace(
        async_playwright=lambda: FakeAsyncPlaywright(chromium),
    )


def _fresh_locks(monkeypatch):
    monkeypatch.setattr(control_panel, "_browser_op_lock", asyncio.Lock())
    monkeypatch.setattr(control_panel, "_login_probe_lock", asyncio.Lock())


@pytest.fixture(autouse=True)
def _reset_states():
    saved_login = dict(control_panel._login_state)
    saved_scrape = dict(control_panel._scrape_state)
    saved_discover = dict(control_panel._discover_state)
    yield
    control_panel._login_state.clear()
    control_panel._login_state.update(saved_login)
    control_panel._scrape_state.clear()
    control_panel._scrape_state.update(saved_scrape)
    control_panel._discover_state.clear()
    control_panel._discover_state.update(saved_discover)


# ── canonical profile ──


def test_web_scraper_uses_paths_browser_profile(monkeypatch, tmp_path):
    profile = str(tmp_path / "profile")
    monkeypatch.setattr(paths, "BROWSER_PROFILE", profile)
    assert web_scraper.get_browser_profile_dir() == profile


def test_web_scraper_has_no_hardcoded_profile_path():
    source = Path(web_scraper.__file__).read_text(encoding="utf-8")
    assert "USER_DATA_DIR" not in source
    assert 'os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "browser_profile")' not in source


def test_scraper_launch_uses_canonical_profile_and_headless_env(monkeypatch, tmp_path):
    profile = str(tmp_path / "profile")
    monkeypatch.setattr(paths, "BROWSER_PROFILE", profile)
    monkeypatch.setenv("HEADLESS", "true")
    monkeypatch.setattr(web_scraper, "init_db", lambda: None)
    monkeypatch.setattr(web_scraper, "get_db", lambda: None)
    chromium = FakeChromium()

    async def scenario():
        scraper = web_scraper.WebChatScraper()
        with patch.object(web_scraper, "async_playwright", lambda: FakeAsyncPlaywright(chromium)):
            await scraper.launch()
        assert chromium.launched[0].user_data_dir == profile
        assert chromium.launched[0].launch_kwargs["headless"] is True
        await scraper.close()

    asyncio.run(scenario())


def test_has_login_detects_non_empty_sessionid():
    chromium = FakeChromium(cookies=[{"name": "sessionid", "value": "abc"}])
    scraper = web_scraper.WebChatScraper()
    scraper.context = FakeContext(cookies=chromium.cookies)
    assert asyncio.run(scraper.has_login()) is True


def test_has_login_false_without_sessionid():
    scraper = web_scraper.WebChatScraper()
    scraper.context = FakeContext(cookies=[])
    assert asyncio.run(scraper.has_login()) is False


def test_worker_close_releases_context_and_playwright():
    chromium = FakeChromium()
    scraper = web_scraper.WebChatScraper()
    scraper.context = FakeContext(events=chromium.events)
    scraper.pw = FakePW(chromium)
    asyncio.run(scraper.close())
    assert scraper.context.closed is True
    assert scraper.pw.stopped is True


def test_extract_supports_require_existing_login_env():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "extract.py").read_text(encoding="utf-8")
    assert "DOUYIN_CHAT_REQUIRE_EXISTING_LOGIN" in source
    assert "has_login" in source


# ── desktop flag and worker environment ──


def test_desktop_main_sets_desktop_flag(monkeypatch):
    import desktop.main as desktop_main

    monkeypatch.delenv("DOUYIN_CHAT_DESKTOP", raising=False)
    observed = {}

    def fake_configure_environment():
        observed["flag"] = os.environ.get("DOUYIN_CHAT_DESKTOP")
        return (str(Path.cwd()), str(Path.cwd()))

    monkeypatch.setattr(desktop_main, "configure_environment", fake_configure_environment)
    monkeypatch.setattr(desktop_main, "run_desktop", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    assert desktop_main.main() == 0
    assert observed["flag"] == "1"


def test_desktop_worker_env_sets_headless_and_quick_fail(monkeypatch):
    monkeypatch.setenv("DOUYIN_CHAT_DESKTOP", "1")
    env = control_panel._worker_subprocess_env()
    assert env["HEADLESS"] == "true"
    assert env["DOUYIN_CHAT_REQUIRE_EXISTING_LOGIN"] == "1"


def test_non_desktop_worker_env_preserves_headless(monkeypatch):
    monkeypatch.delenv("DOUYIN_CHAT_DESKTOP", raising=False)
    monkeypatch.delenv("HEADLESS", raising=False)
    env = control_panel._worker_subprocess_env()
    assert "HEADLESS" not in env
    assert "DOUYIN_CHAT_REQUIRE_EXISTING_LOGIN" not in env


# ── desktop login flow ──


def test_desktop_login_uses_headed_window_and_canonical_profile(monkeypatch, tmp_path):
    profile = str(tmp_path / "profile")
    monkeypatch.setattr(paths, "BROWSER_PROFILE", profile)
    chromium = FakeChromium(cookies=[{"name": "sessionid", "value": "abc"}])

    async def scenario():
        control_panel._login_state.update(
            {"status": "starting", "interaction_mode": "native_window"}
        )
        with patch.dict(
            sys.modules,
            {"playwright.async_api": _fake_async_playwright_module(chromium)},
        ):
            await control_panel._login_flow_desktop()

    asyncio.run(scenario())

    ctx = chromium.launched[0]
    assert ctx.user_data_dir == profile
    assert ctx.user_data_dir == web_scraper.get_browser_profile_dir()
    assert ctx.launch_kwargs["headless"] is False
    assert control_panel._login_state["status"] == "logged_in"
    assert control_panel._login_state["_context"] is None
    assert control_panel._login_state["_pw"] is None
    # context closed and playwright stopped BEFORE status became logged_in
    assert chromium.events == ["ctx_closed", "pw_stopped"]


def test_desktop_login_window_closed_without_login(monkeypatch, tmp_path):
    chromium = FakeChromium(cookies=[])

    async def scenario():
        control_panel._login_state.update(
            {"status": "starting", "interaction_mode": "native_window"}
        )
        with patch.dict(
            sys.modules,
            {"playwright.async_api": _fake_async_playwright_module(chromium)},
        ):
            task = asyncio.create_task(control_panel._login_flow_desktop())
            await asyncio.sleep(0.1)
            chromium.launched[0].fire_close()
            await task

    asyncio.run(scenario())
    assert control_panel._login_state["status"] == "cancelled"
    assert "未完成登录" in control_panel._login_state["message"]
    assert chromium.launched[0].closed is True


def test_cancel_login_closes_context_and_releases_playwright(monkeypatch, tmp_path):
    monkeypatch.setattr(control_panel, "_browser_op_lock", asyncio.Lock())
    chromium = FakeChromium(cookies=[])

    async def scenario():
        control_panel._login_state.update(
            {"status": "starting", "interaction_mode": "native_window"}
        )
        with patch.dict(
            sys.modules,
            {"playwright.async_api": _fake_async_playwright_module(chromium)},
        ):
            task = asyncio.create_task(control_panel._login_flow_desktop())
            await asyncio.sleep(0.1)
            await control_panel.login_cancel()
            await task

    asyncio.run(scenario())
    assert control_panel._login_state["status"] == "idle"
    assert control_panel._login_state["_context"] is None
    assert control_panel._login_state["_pw"] is None
    assert chromium.launched[0].closed is True


# ── web/docker login flow stays screenshot-based ──


def test_web_login_keeps_headless_screenshot_mode(monkeypatch, tmp_path):
    monkeypatch.delenv("DOUYIN_CHAT_DESKTOP", raising=False)
    monkeypatch.setattr(paths, "BROWSER_PROFILE", str(tmp_path / "profile"))
    chromium = FakeChromium(cookies=[])

    async def scenario():
        control_panel._login_state.update({"status": "starting"})
        with patch.dict(
            sys.modules,
            {"playwright.async_api": _fake_async_playwright_module(chromium)},
        ):
            task = asyncio.create_task(control_panel._login_flow())
            # Wait until the flow reaches the screenshot-polling state, then
            # cancel from the test so the 180s poll loop exits quickly.
            for _ in range(60):
                if control_panel._login_state["status"] == "waiting_scan":
                    break
                await asyncio.sleep(0.1)
            control_panel._login_state["status"] = "idle"
            await task

    asyncio.run(scenario())
    ctx = chromium.launched[0]
    assert ctx.launch_kwargs["headless"] is True
    assert control_panel._login_state["interaction_mode"] == "screenshot"
    assert control_panel._login_state["screenshot"] is not None


def test_probe_detects_existing_sessionid_after_restart(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "placeholder").write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "BROWSER_PROFILE", str(profile))
    monkeypatch.setattr(control_panel, "_login_probe_lock", asyncio.Lock())
    chromium = FakeChromium(cookies=[{"name": "sessionid", "value": "ok"}])

    with patch.dict(
        sys.modules,
        {"playwright.async_api": _fake_async_playwright_module(chromium)},
    ):
        result = asyncio.run(control_panel._probe_login_state())

    assert result["status"] == "logged_in"
    assert result["has_cookies"] is True


def test_probe_skipped_while_login_window_active(monkeypatch):
    control_panel._login_state.update(
        {"status": "waiting_login", "interaction_mode": "native_window"}
    )
    result = asyncio.run(control_panel._probe_login_state())
    assert result["status"] == "busy"
    assert result["has_cookies"] is False


# ── concurrency guards ──


def test_refresh_during_login_conflict(monkeypatch):
    _fresh_locks(monkeypatch)
    control_panel._login_state.update({"status": "waiting_login"})
    resp = asyncio.run(control_panel.refresh_conversations())
    assert resp.status_code == 409
    assert control_panel._discover_state["status"] != "running"


def test_scrape_during_login_conflict(monkeypatch):
    _fresh_locks(monkeypatch)
    control_panel._login_state.update({"status": "waiting_login"})
    resp = asyncio.run(
        control_panel.start_scrape(control_panel.ScrapeRequest(incremental=True))
    )
    assert resp.status_code == 409
    assert control_panel._scrape_state["status"] != "running"


def test_login_during_scrape_conflict(monkeypatch):
    _fresh_locks(monkeypatch)
    control_panel._scrape_state.update({"status": "running"})
    resp = asyncio.run(control_panel.login_start())
    assert resp.status_code == 409


def test_login_during_refresh_conflict(monkeypatch):
    _fresh_locks(monkeypatch)
    control_panel._discover_state.update({"status": "running"})
    resp = asyncio.run(control_panel.login_start())
    assert resp.status_code == 409


def test_cookie_import_during_login_conflict(monkeypatch):
    _fresh_locks(monkeypatch)
    control_panel._login_state.update({"status": "waiting_login"})
    resp = asyncio.run(
        control_panel.login_cookie_import(
            control_panel.CookieImportRequest(cookies="sessionid=abc")
        )
    )
    assert resp.status_code == 409


def test_cookie_import_during_scrape_conflict(monkeypatch):
    _fresh_locks(monkeypatch)
    control_panel._scrape_state.update({"status": "running"})
    resp = asyncio.run(
        control_panel.login_cookie_import(
            control_panel.CookieImportRequest(cookies="sessionid=abc")
        )
    )
    assert resp.status_code == 409


# ── login prechecks prevent worker startup when logged out ──


async def _no_login_probe():
    return {"status": "no_profile", "has_cookies": False}


def test_unlogged_refresh_does_not_start_worker(monkeypatch):
    _fresh_locks(monkeypatch)
    monkeypatch.setattr(control_panel, "_probe_login_state", _no_login_probe)
    started = []
    monkeypatch.setattr(control_panel, "_run_discover", lambda cmd: started.append(cmd))
    resp = asyncio.run(control_panel.refresh_conversations())
    assert resp.status_code == 400
    assert started == []


def test_unlogged_scrape_does_not_start_worker(monkeypatch):
    _fresh_locks(monkeypatch)
    monkeypatch.setattr(control_panel, "_probe_login_state", _no_login_probe)
    started = []
    monkeypatch.setattr(control_panel, "_run_scrape", lambda cmd: started.append(cmd))
    resp = asyncio.run(
        control_panel.start_scrape(control_panel.ScrapeRequest(incremental=True))
    )
    assert resp.status_code == 400
    assert started == []


# ── panel UI contract and data hygiene ──


def test_panel_desktop_login_hides_screenshot_interaction():
    html = control_panel.PANEL_HTML
    assert 'id="loginDesktopHint"' in html
    assert "applyLoginInteractionMode" in html
    assert "native_window" in html
    assert "screenshot.style.display = 'none';" in html


def test_panel_web_login_keeps_screenshot_interaction():
    html = control_panel.PANEL_HTML
    assert 'id="loginScreenshot"' in html
    assert "imgMouseDown" in html
    assert "sendLoginKey" in html
    assert "waiting_scan" in html


def test_panel_i18n_keys_present_in_both_languages():
    html = control_panel.PANEL_HTML
    for key in (
        "desktopLoginBtn",
        "desktopLoginHint",
        "waitingLogin",
        "loginCancelled",
        "loginWindowClosed",
    ):
        assert html.count(key + ":") == 2, key


def test_login_status_does_not_leak_cookie_or_profile(monkeypatch, tmp_path):
    profile = str(tmp_path / "profile")
    monkeypatch.setattr(paths, "BROWSER_PROFILE", profile)
    control_panel._login_state.update(
        {
            "status": "waiting_login",
            "interaction_mode": "native_window",
            "message": "请在弹出的抖音浏览器窗口中完成登录",
        }
    )
    status = asyncio.run(control_panel.login_status())
    blob = json.dumps(status, ensure_ascii=False)
    assert "sessionid" not in blob
    assert profile not in blob


def test_profile_path_not_under_bundle_or_frontend(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    data_dir = str(tmp_path / "data")
    code = (
        "import os;"
        f"os.environ['DOUYIN_CHAT_DATA_DIR'] = {data_dir!r};"
        "from common import paths;"
        "print(paths.BROWSER_PROFILE)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, result.stderr
    profile = result.stdout.strip()
    assert profile == os.path.join(data_dir, "browser_profile")
    assert "_internal" not in profile
    assert "frontend" not in profile
    assert "\\dist\\" not in profile
