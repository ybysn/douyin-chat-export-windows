"""Conversation selection must fail closed before any destructive scrape step."""

import asyncio

from common.db import connect, upsert_conversation
from extractor import web_scraper
from extractor.web_scraper import WebChatScraper
from tests.conftest import insert_message


class _Element:
    def __init__(self, page, index):
        self.page = page
        self.index = index

    async def click(self):
        self.page.clicked.append(self.index)


class SelectionPage:
    def __init__(self, names):
        self.rows = [
            {
                "index": index,
                "position": index,
                "nickname": name,
                "full_text": name,
                "text": name,
            }
            for index, name in enumerate(names)
        ]
        self.clicked = []

    async def evaluate(self, script, arg=None):
        if "return Array.from(items).map" in script:
            return [dict(row) for row in self.rows]
        if "scrollTop += 400" in script:
            return True
        return None

    async def query_selector_all(self, selector):
        return [_Element(self, index) for index in range(len(self.rows))]


class VirtualSelectionPage(SelectionPage):
    def __init__(self, screens):
        self.screens = []
        for screen_index, names in enumerate(screens):
            self.screens.append([
                {
                    "index": index,
                    "position": screen_index * 100 + index,
                    "nickname": name,
                    "full_text": name,
                    "text": name,
                }
                for index, name in enumerate(names)
            ])
        self.screen_index = 0
        self.clicked = []

    @property
    def rows(self):
        return self.screens[self.screen_index]

    async def evaluate(self, script, arg=None):
        if "return Array.from(items).map" in script:
            return [dict(row) for row in self.rows]
        if "scrollTop = 0" in script:
            self.screen_index = 0
            return None
        if "scrollTop += 400" in script:
            if self.screen_index + 1 < len(self.screens):
                self.screen_index += 1
                return False
            return True
        return None

    async def query_selector_all(self, selector):
        page = self

        class VirtualElement:
            def __init__(self, index):
                self.index = index

            async def click(self):
                page.clicked.append((page.screen_index, self.index))

        return [VirtualElement(index) for index in range(len(self.rows))]


def _run_selection(names, target, monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(web_scraper.asyncio, "sleep", no_sleep)
    scraper = WebChatScraper()
    scraper.page = SelectionPage(names)
    result = asyncio.run(scraper._find_and_click_conversation(target))
    return result, scraper.page.clicked


def test_exact_match_wins_when_longer_fuzzy_name_is_first(monkeypatch):
    result, clicked = _run_selection(["小D的其他名称", "小D"], "小D", monkeypatch)

    assert result["found"] is True
    assert result["match_type"] == "exact"
    assert result["text"] == "小D"
    assert clicked == [1]


def test_exact_match_wins_when_it_is_first(monkeypatch):
    result, clicked = _run_selection(["小D", "小D的其他名称"], "小D", monkeypatch)

    assert result["found"] is True
    assert result["match_type"] == "exact"
    assert result["text"] == "小D"
    assert clicked == [0]


def test_exact_match_in_later_virtual_view_wins_over_earlier_fuzzy(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(web_scraper.asyncio, "sleep", no_sleep)
    scraper = WebChatScraper()
    scraper.page = VirtualSelectionPage([["小D的其他名称"], ["小D"]])

    result = asyncio.run(scraper._find_and_click_conversation("小D"))

    assert result["match_type"] == "exact"
    assert result["text"] == "小D"
    assert scraper.page.clicked == [(1, 0)]


def test_unique_fuzzy_candidate_is_allowed(monkeypatch):
    result, clicked = _run_selection(["测试账号"], "测试", monkeypatch)

    assert result["found"] is True
    assert result["match_type"] == "fuzzy-unique"
    assert result["candidates"] == ["测试账号"]
    assert clicked == [0]


def test_multiple_fuzzy_candidates_are_ambiguous_and_not_clicked(monkeypatch):
    result, clicked = _run_selection(["测试A", "测试B"], "测试", monkeypatch)

    assert result["found"] is False
    assert result["ambiguous"] is True
    assert result["candidates"] == ["测试A", "测试B"]
    assert clicked == []


def test_fuzzy_candidates_across_virtual_views_are_ambiguous(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(web_scraper.asyncio, "sleep", no_sleep)
    scraper = WebChatScraper()
    scraper.page = VirtualSelectionPage([["测试A"], ["测试B"]])

    result = asyncio.run(scraper._find_and_click_conversation("测试"))

    assert result["found"] is False
    assert result["ambiguous"] is True
    assert result["candidates"] == ["测试A", "测试B"]
    assert scraper.page.clicked == []


def test_identically_named_fuzzy_rows_are_still_two_candidates(monkeypatch):
    result, clicked = _run_selection(["测试账号", "测试账号"], "测试", monkeypatch)

    assert result["found"] is False
    assert result["ambiguous"] is True
    assert result["candidates"] == ["测试账号", "测试账号"]
    assert clicked == []


class ExtractionPage:
    def __init__(self, active_name, conv_id="conv-real"):
        self.active_name = active_name
        self.conv_id = conv_id
        self.conv_id_reads = 0

    async def evaluate(self, script, arg=None):
        if "window.conversationStore" in script:
            self.conv_id_reads += 1
            return self.conv_id
        if "curConversation" in script:
            return {"nickname": self.active_name, "full_text": self.active_name}
        raise AssertionError("unexpected page evaluation")


def _exact_result(name):
    return {
        "found": True,
        "ambiguous": False,
        "index": 0,
        "text": name,
        "nickname": name,
        "full_text": name,
        "match_type": "exact",
        "matched_by": "nickname",
        "candidates": [name],
    }


def test_active_name_mismatch_stops_before_conv_id_delete_or_api(monkeypatch, capsys):
    async def no_sleep(_seconds):
        return None

    async def ensure_loaded():
        return 1

    async def find_conversation(_target):
        return _exact_result("小D")

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("message acquisition must not run")

    class NoDatabaseAccess:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("database must not be touched")

    monkeypatch.setattr(web_scraper.asyncio, "sleep", no_sleep)
    scraper = WebChatScraper(incremental=False)
    scraper.page = ExtractionPage("小D的其他名称")
    scraper._db_conn = NoDatabaseAccess()
    scraper._ensure_conv_list_loaded = ensure_loaded
    scraper._find_and_click_conversation = find_conversation
    scraper._acquire_short_id = must_not_run
    scraper._api_fetch_all_messages = must_not_run

    asyncio.run(scraper._extract_conversation(0, {"name": "小D", "nickname": "小D"}))

    assert scraper.page.conv_id_reads == 0
    assert "会话选择校验失败：目标「小D」，实际「小D的其他名称」" in capsys.readouterr().out


class CountingConnection:
    def __init__(self):
        self.delete_calls = 0

    def execute(self, sql, params=()):
        assert sql.startswith("DELETE FROM messages")
        self.delete_calls += 1
        return type("Cursor", (), {"rowcount": 0})()

    def commit(self):
        return None


def test_duplicate_real_conversation_id_is_skipped(monkeypatch, capsys):
    async def no_sleep(_seconds):
        return None

    async def ensure_loaded():
        return 1

    async def find_conversation(_target):
        return _exact_result("小D")

    async def no_op(*_args, **_kwargs):
        return None

    api_calls = []

    async def acquire_short_id(_conv_id, _name):
        return "short-id"

    async def fetch_messages(*args, **kwargs):
        api_calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(web_scraper.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(web_scraper, "upsert_conversation", lambda *_args, **_kwargs: None)
    scraper = WebChatScraper(incremental=False)
    scraper.page = ExtractionPage("小D", conv_id="same-conv-id")
    scraper._db_conn = CountingConnection()
    scraper._ensure_conv_list_loaded = ensure_loaded
    scraper._find_and_click_conversation = find_conversation
    scraper._backup_conv_messages = lambda _conv_id: 0
    scraper._restore_conv_messages_if_empty = lambda _conv_id, _count: None
    scraper._extract_and_save_conv_avatar = no_op
    scraper._extract_and_save_user_info = no_op
    scraper._acquire_short_id = acquire_short_id
    scraper._api_fetch_all_messages = fetch_messages

    conversation = {"name": "小D", "nickname": "小D"}
    asyncio.run(scraper._extract_conversation(0, conversation))
    asyncio.run(scraper._extract_conversation(1, conversation))

    assert scraper._db_conn.delete_calls == 1
    assert len(api_calls) == 1
    assert scraper.processed_conv_ids == {"same-conv-id"}
    assert "检测到重复会话ID，已跳过重复采集" in capsys.readouterr().out


def test_full_mode_ambiguous_selection_preserves_existing_messages(temp_db, monkeypatch):
    async def ensure_loaded():
        return 2

    async def ambiguous(_target):
        return {
            "found": False,
            "ambiguous": True,
            "index": -1,
            "text": "",
            "candidates": ["测试A", "测试B"],
        }

    conn = connect(foreign_keys=True)
    upsert_conversation(conn, "existing-conv", name="测试")
    insert_message(conn, "old-message", "existing-conv", seq=1, content="保留我")
    conn.commit()

    scraper = WebChatScraper(incremental=False)
    scraper._db_conn = conn
    scraper._ensure_conv_list_loaded = ensure_loaded
    scraper._find_and_click_conversation = ambiguous

    asyncio.run(scraper._extract_conversation(0, {"name": "测试", "nickname": "测试"}))

    rows = conn.execute(
        "SELECT msg_id, content FROM messages WHERE conv_id = ?", ("existing-conv",)
    ).fetchall()
    assert [tuple(row) for row in rows] == [("old-message", "保留我")]
