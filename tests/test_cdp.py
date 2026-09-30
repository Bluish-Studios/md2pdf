"""The DevTools client and the PDF readers, with a fake WebSocket and fake PDFs instead of Edge."""
from __future__ import annotations

import asyncio
import json
import types

import pytest

import md2pdf


class FakeSocket:
    """Answers each sent command with replies[method](msg), and can push events. Iteration ends on close()."""

    def __init__(self, replies=None):
        self.replies, self.sent, self.inbox, self.closed = replies or {}, [], asyncio.Queue(), False

    async def send(self, raw):
        if self.closed:
            raise md2pdf.ConnectionClosed(None, None)
        msg = json.loads(raw)
        self.sent.append(msg)
        reply = self.replies.get(msg["method"], {"result": {}})
        if reply is not None:
            await self.inbox.put(json.dumps({"id": msg["id"], **(reply(msg) if callable(reply) else reply)}))

    def push(self, event):
        self.inbox.put_nowait(json.dumps(event))

    def close(self):
        self.closed = True
        self.inbox.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item


def run(coro):
    return asyncio.run(coro)


def test_send_returns_results_and_passes_sessions():
    async def go():
        ws = FakeSocket({"Browser.getVersion": {"result": {"product": "Edg/1"}}})
        cdp = md2pdf.Cdp(ws)
        pump = asyncio.create_task(cdp.pump())
        assert await cdp.send("Browser.getVersion") == {"product": "Edg/1"}
        assert await cdp.send("Page.enable", session="S1") == {}
        ws.close()
        await pump
        return ws.sent
    sent = run(go())
    assert sent[1] == {"id": 2, "method": "Page.enable", "params": {}, "sessionId": "S1"}


def test_send_raises_protocol_errors():
    async def go():
        ws = FakeSocket({"Bad.method": {"error": {"message": "not found"}}, "Worse.method": {"error": "plain"}})
        cdp = md2pdf.Cdp(ws)
        pump = asyncio.create_task(cdp.pump())
        with pytest.raises(RuntimeError, match="Bad.method: not found"):
            await cdp.send("Bad.method")
        with pytest.raises(RuntimeError, match="Worse.method: plain"):
            await cdp.send("Worse.method")
        ws.close()
        await pump
    run(go())


def test_send_times_out():
    async def go():
        cdp = md2pdf.Cdp(FakeSocket({"Slow.method": None}))
        with pytest.raises(TimeoutError, match="did not answer Slow.method within 0.05 s"):
            await cdp.send("Slow.method", timeout=0.05)
        assert cdp.pending == {}
    run(go())


def test_send_on_a_closed_socket():
    async def go():
        ws = FakeSocket()
        ws.closed = True
        with pytest.raises(ConnectionError, match="lost the connection"):
            await md2pdf.Cdp(ws).send("Page.enable")
    run(go())


def test_events_reach_the_matching_waiter_and_lost_connections_fail_the_rest():
    async def go():
        ws = FakeSocket({"Never.answered": None})
        cdp = md2pdf.Cdp(ws)
        pump = asyncio.create_task(cdp.pump())
        loaded = cdp.expect("Page.loadEventFired", "S1")
        other = cdp.expect("Page.loadEventFired", "S2")
        abandoned = cdp.expect("Page.frameNavigated", "S1")
        abandoned.cancel()
        ws.push({"method": "Page.loadEventFired", "sessionId": "S1", "params": {"timestamp": 1}})
        assert await loaded == {"timestamp": 1}
        pending = asyncio.create_task(cdp.send("Never.answered"))
        await asyncio.sleep(0.01)
        ws.push({"id": 999, "result": {}})  # a reply nobody waits for
        ws.close()
        await pump
        with pytest.raises(ConnectionError):
            await other
        with pytest.raises(ConnectionError):
            await pending
    run(go())


def test_evaluate_returns_values_and_raises_page_errors():
    replies = iter([{"result": {"result": {"value": 42}}},
                    {"result": {"exceptionDetails": {"exception": {"description": "ReferenceError: x"}}}},
                    {"result": {"exceptionDetails": {"text": "Uncaught"}}}])

    async def go():
        ws = FakeSocket({"Runtime.evaluate": lambda msg: next(replies)})
        cdp = md2pdf.Cdp(ws)
        pump = asyncio.create_task(cdp.pump())
        assert await md2pdf.evaluate(cdp, "S", "6*7") == 42
        with pytest.raises(RuntimeError, match="page script failed: ReferenceError: x"):
            await md2pdf.evaluate(cdp, "S", "x")
        with pytest.raises(RuntimeError, match="page script failed: Uncaught"):
            await md2pdf.evaluate(cdp, "S", "y")
        ws.close()
        await pump
    run(go())


# --- Browser shutdown ---------------------------------------------------------------------------------------------

class Proc:
    def __init__(self, hang=False):
        self.hang, self.killed = hang, False

    def poll(self):
        return None

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise md2pdf.subprocess.TimeoutExpired("msedge", timeout)
        return 0

    def kill(self):
        self.killed = True


def test_browser_close_kills_a_hung_edge_and_removes_the_profile(tmp_path):
    async def go():
        b = md2pdf.Browser("msedge", tmp_path)
        b.proc, b.profile = Proc(hang=True), tmp_path / "edge-profile"
        b.profile.mkdir()
        await b.close()
        return b
    b = run(go())
    assert b.proc.killed and not b.profile.exists()
    assert not b.alive()


def test_browser_start_reports_connection_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(md2pdf, "launch_edge", lambda exe, work: (Proc(), "ws://127.0.0.1:1/x", tmp_path / "p"))

    async def refuse(url, **kwargs):
        raise OSError("connection refused")
    monkeypatch.setattr(md2pdf, "ws_connect", refuse)
    with pytest.raises(md2pdf.SetupError, match="could not connect to headless Edge: connection refused"):
        run(md2pdf.Browser("msedge", tmp_path).start())


# --- reading the printed PDF --------------------------------------------------------------------------------------

class Page:
    def __init__(self, text, annots=None):
        self.text, self.annots = text, annots

    def extract_text(self):
        return self.text

    def get(self, key):
        return Ref(self.annots) if key == "/Annots" and self.annots is not None else None


class Ref:
    def __init__(self, obj):
        self.obj = obj

    def get_object(self):
        return self.obj


def test_runt_pages_finds_chapters_ending_on_a_nearly_empty_page():
    reader = types.SimpleNamespace(pages=[Page("x" * 900), Page("tiny"), Page("x" * 900), Page("x" * 900)])
    found = {"one": 1, "two": 3}
    assert md2pdf.runt_pages(reader, ["one", "two"], found, "Footer", 100) == [("one", 2, 4 - len("Footer2/4"))]
    assert md2pdf.runt_pages(reader, ["one"], found, "Footer", 100) == []
    assert md2pdf.runt_pages(reader, ["one", "gone"], found, "Footer", 100) == []
    assert md2pdf.runt_pages(reader, ["one", "two"], {"one": 1, "two": 1}, "F", 100) == []


def test_dest_pages_falls_back_to_bookmark_titles():
    class Reader:
        named_destinations = {"/intro": "d1", "/broken": "bad"}

        @property
        def outline(self):
            return [Item("Design  Notes"), [Item("Sub")]]

        def get_destination_page_number(self, dest):
            if dest == "bad":
                raise ValueError("no page")
            return {"d1": 0, "Design  Notes": 4, "Sub": 5}[dest if isinstance(dest, str) else dest.title]

    entries = [{"id": "intro", "text": "Intro"}, {"id": "design", "text": "Design Notes"}, {"id": "gone", "text": "Gone"}]
    assert md2pdf.dest_pages(Reader(), entries) == {"intro": 1, "design": 5, "gone": None}


class Item:
    def __init__(self, title):
        self.title = title


def test_pdf_summary_counts_links_and_bookmarks():
    link = lambda **kw: Ref({"/Subtype": "/Link", **kw})
    annots = [link(**{"/Dest": "x"}), link(**{"/A": Ref({"/S": "/GoTo"})}), link(**{"/A": Ref({"/S": "/URI"})}),
              Ref({"/Subtype": "/Text"})]
    reader = types.SimpleNamespace(pages=[Page("", annots), Page("")], outline=[Item("a"), [Item("b"), Item("c")]])
    assert md2pdf.pdf_summary(reader) == {"pages": 2, "bookmarks": 3, "internal_links": 2, "external_links": 1}


def test_resolved_defaults():
    assert md2pdf.resolved(None, "/Annots", []) == []
    assert md2pdf.resolved({"/A": Ref({"/S": "/URI"})}, "/A") == {"/S": "/URI"}
