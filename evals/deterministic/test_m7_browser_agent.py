"""Deterministic M7 browser runtime and agent integration contracts."""

from __future__ import annotations

import json

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from tieru.config import Settings
from tieru.tools.browser import RestrictedBrowser, make_tools
from tieru.tools.registry import ToolRegistry


class FakeResponse:
    status = 200


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def inner_text(self, **_kwargs):
        if self.page.timeout:
            raise TimeoutError("page timed out")
        return self.page.content[self.page.url]

    def click(self, **_kwargs):
        if self.selector == "[invalid":
            raise ValueError("invalid selector")
        self.page.calls.append(("click", self.selector))
        if self.selector == "a":
            self.page.url = "https://example.com/next"

    def fill(self, value, **_kwargs):
        self.page.calls.append(("fill", self.selector, value))


class FakeMouse:
    def __init__(self, page):
        self.page = page

    def wheel(self, x, y):
        self.page.calls.append(("wheel", x, y))


class FakePage:
    def __init__(self, *, timeout=False):
        self.url = "https://example.com/"
        self.timeout = timeout
        self.calls = []
        self.content = {
            "https://example.com/": "Example Domain. Follow the link.",
            "https://example.com/next": "The linked page explains the example domain.",
        }
        self.mouse = FakeMouse(self)

    def goto(self, url, **_kwargs):
        self.calls.append(("goto", url))
        self.url = url
        return FakeResponse()

    def title(self):
        return "Next page" if self.url.endswith("/next") else "Example Domain"

    def locator(self, selector):
        return FakeLocator(self, selector)

    def screenshot(self, **_kwargs):
        return b"bounded-png"

    def go_back(self, **_kwargs):
        self.url = "https://example.com/"
        return FakeResponse()


class FakeResource:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def fake_browser(tmp_path, *, timeout=False):
    browser = RestrictedBrowser(
        Settings(
            home=tmp_path,
            browser_allowed_domains=("example.com",),
            browser_timeout_seconds=1,
        )
    )
    browser._page = FakePage(timeout=timeout)
    browser.validate_url = lambda url: url
    return browser


def registry_for(browser, *, approve=True):
    registry = ToolRegistry(
        approval_handler=lambda _request: approve,
        trust_context={"browser_domains": ["example.com"]},
    )
    for tool in make_tools(browser):
        registry.register(tool)
    return registry


def test_open_read_click_and_read_use_one_bounded_session(tmp_path):
    browser = fake_browser(tmp_path)

    opened = json.loads(browser.open("https://example.com/"))
    first = json.loads(browser.read(max_chars=200))
    clicked = json.loads(browser.click("a"))
    second = json.loads(browser.read(max_chars=200))

    assert opened["url"] == first["url"] == "https://example.com/"
    assert clicked["url"] == second["url"] == "https://example.com/next"
    assert "Example Domain" in first["content"]
    assert "linked page" in second["content"]
    assert "UNTRUSTED WEB CONTENT" in second["content"]


def test_browser_type_fills_only_and_never_submits(tmp_path):
    browser = fake_browser(tmp_path)

    result = json.loads(browser.type("#query", "Tieru"))

    assert result["submitted"] is False
    assert browser._page.calls == [("fill", "#query", "Tieru")]
    assert "Tieru" not in json.dumps(result)


def test_invalid_selector_and_timeout_are_controlled_tool_failures(tmp_path):
    invalid_browser = fake_browser(tmp_path / "invalid")
    invalid = json.loads(
        registry_for(invalid_browser).execute("browser_click", {"selector": "[invalid"})
    )
    timeout_browser = fake_browser(tmp_path / "timeout", timeout=True)
    timed_out = json.loads(registry_for(timeout_browser).execute("browser_read", {}))

    assert invalid["error"]["code"] == "tool_execution_error"
    assert timed_out["error"]["code"] == "tool_execution_error"
    assert invalid_browser._page is None
    assert timeout_browser._page is None


def test_side_effect_browser_actions_require_permission(tmp_path):
    browser = fake_browser(tmp_path)
    registry = registry_for(browser, approve=False)

    clicked = json.loads(registry.execute("browser_click", {"selector": "a"}))
    typed = json.loads(
        registry.execute("browser_type", {"selector": "#query", "value": "private"})
    )

    assert clicked["error"]["code"] == typed["error"]["code"] == "tool_permission_denied"
    assert browser._page.calls == []


def test_browser_tool_contract_read_only_actions_and_schema(tmp_path):
    tools = {tool.name: tool for tool in make_tools(fake_browser(tmp_path))}
    required = {
        "browser_open",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_screenshot",
        "browser_back",
        "browser_close",
    }

    assert required <= tools.keys()
    for name in (
        "browser_open",
        "browser_read",
        "browser_scroll",
        "browser_screenshot",
        "browser_back",
        "browser_close",
    ):
        assert tools[name].read_only is True
        assert tools[name].default_policy == "allow"
    assert tools["browser_click"].default_policy == "confirm"
    assert tools["browser_type"].default_policy == "confirm"
    assert tools["browser_type"].sensitive_args == ("value",)
    assert tools["browser_screenshot"].capabilities == ("browser",)
    assert all(tool.input_schema.get("additionalProperties") is False for tool in tools.values())


def test_browser_close_cleans_every_session_resource(tmp_path):
    browser = fake_browser(tmp_path)
    context = FakeResource()
    engine = FakeResource()
    playwright = FakePlaywright()
    browser._context = context
    browser._browser = engine
    browser._playwright = playwright
    browser.actions = 5

    result = json.loads(browser.close())

    assert result == {"status": "closed"}
    assert context.closed and engine.closed and playwright.stopped
    assert browser._page is browser._context is browser._browser is browser._playwright is None
    assert browser.actions == 0


def test_agent_browser_chain_synthesizes_observation_and_replay_records_actions(
    monkeypatch, tmp_path
):
    state = {"url": "https://example.com/"}

    def open_page(_browser, url):
        state["url"] = url
        return json.dumps({"url": url, "title": "Example Domain", "status": "ok"})

    def read_page(_browser, max_chars=12000):
        del max_chars
        content = (
            "Example Domain. Follow the link."
            if state["url"].endswith(".com/")
            else "The linked page explains the example domain."
        )
        return json.dumps(
            {
                "url": state["url"],
                "title": "Example Domain" if state["url"].endswith(".com/") else "Next page",
                "content": content,
                "status": "ok",
            }
        )

    def click_link(_browser, selector):
        assert selector == "a"
        state["url"] = "https://example.com/next"
        return json.dumps({"url": state["url"], "title": "Next page", "status": "ok"})

    monkeypatch.setattr(RestrictedBrowser, "open", open_page)
    monkeypatch.setattr(RestrictedBrowser, "read", read_page)
    monkeypatch.setattr(RestrictedBrowser, "click", click_link)
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    client = ScriptedClient(
        [
            gate,
            response(
                [tool_block("browser_open", {"url": "https://example.com/"}, "open")],
                "tool_use",
            ),
            response([tool_block("browser_read", {}, "read-1")], "tool_use"),
            response([tool_block("browser_click", {"selector": "a"}, "click")], "tool_use"),
            response([tool_block("browser_read", {}, "read-2")], "tool_use"),
            response([text_block("done")]),
            response(
                [
                    text_block(
                        "The linked page explains the example domain. "
                        "Source: https://example.com/next"
                    )
                ]
            ),
        ]
    )
    app = make_waku(
        tmp_path / "home",
        client=client,
        browser_enabled=True,
        browser_allowed_domains=("example.com",),
    )

    result = app.respond("Open Example Domain, follow its link, and explain the destination.")
    events = app.replay.get_events(result.run_id)
    completed = {
        event["tool"]
        for event in events
        if event["event_type"] == "tool_completed"
    }

    assert "linked page explains" in result.reply
    assert "https://example.com/next" in result.reply
    assert {"browser_open", "browser_read", "browser_click"} <= completed
    assert any(event["event_type"] == "trust_decision" for event in events)
    app.close()
