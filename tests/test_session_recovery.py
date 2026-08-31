"""Session recovery: an expired session must not silently halve a long crawl."""

import httpx
import pytest

from okpos_cli.client import OkposClient, SessionExpired, looks_like_login_page
from okpos_cli.config import Config
from okpos_cli.screen import ScreenSpec
from okpos_cli.throttle import HumanThrottle

LOGIN_PAGE = """<html><body>
<form id='loginForm' action='login_check.jsp'>
<input name='user_id'></form></body></html>"""

SCREEN_HTML = """<form id='form1'>
<input type='hidden' name='S_CONTROLLER' value='sale.sale.x010'>
<input type='hidden' name='abc-123' value='tok-1'>
</form><script>{SaveName:"A"}</script>"""

OK_JSON = '{"Data":[{"A":"1"}],"Result":{"Message":"조회완료","Code":0}}'


def _cfg() -> Config:
    return Config(user_id="u", password="p", base_url="https://x.test",
                  login_path="/login/login_form.jsp", pg_dsn=None, max_rps=1000)


class _FakeSession:
    """Stands in for auth.Session without touching the network."""

    def __init__(self, handler, token="tok-session"):
        self.client = httpx.Client(
            base_url="https://x.test", transport=httpx.MockTransport(handler)
        )
        self.token_key, self.token_val = "sess", token
        self.company = "test"
        self.closed = False

    @property
    def csrf(self):
        return {self.token_key: self.token_val}

    def close(self):
        self.closed = True
        self.client.close()


def _spec() -> ScreenSpec:
    return ScreenSpec(path="/sale/sale/x010.jsp", controller="sale.sale.x010",
                      fields={"S_CONTROLLER": "sale.sale.x010", "abc-123": "tok-1"},
                      columns=["A"])


def test_login_page_is_recognised():
    assert looks_like_login_page(LOGIN_PAGE) is True
    assert looks_like_login_page(SCREEN_HTML) is False
    # Only the head of the body is scanned, so a late mention does not count.
    assert looks_like_login_page("x" * 5000 + "login_form.jsp") is False


def test_expired_session_triggers_relogin_and_the_call_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("SheetAction"):
            calls["n"] += 1
            # First attempt hits a dead session, second succeeds.
            if calls["n"] == 1:
                return httpx.Response(200, text=LOGIN_PAGE)
            return httpx.Response(200, text=OK_JSON,
                                  headers={"Content-Type": "text/json"})
        return httpx.Response(200, text=SCREEN_HTML)

    client = OkposClient(_FakeSession(handler), HumanThrottle(1000), _cfg())
    replacement = _FakeSession(handler, token="tok-2")
    monkeypatch.setattr("okpos_cli.client.login", lambda cfg, th: replacement)

    result = client.search(_spec(), 1)

    assert result.ok and result.rows == [{"A": "1"}]
    assert client.relogin_count == 1
    assert client.session is replacement


def test_relogin_clears_cached_screens(monkeypatch):
    """Cached specs carry the dead session's CSRF field and must be dropped."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SCREEN_HTML)

    client = OkposClient(_FakeSession(handler), HumanThrottle(1000), _cfg())
    client.get_screen("/sale/sale/x010.jsp")
    assert client._screens

    monkeypatch.setattr("okpos_cli.client.login", lambda cfg, th: _FakeSession(handler))
    client.relogin()
    assert client._screens == {}


def test_without_config_expiry_is_reported_not_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LOGIN_PAGE)

    client = OkposClient(_FakeSession(handler), HumanThrottle(1000), config=None)
    with pytest.raises(SessionExpired):
        client.search(_spec(), 1)


def test_relogin_attempts_are_capped(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LOGIN_PAGE)

    client = OkposClient(_FakeSession(handler), HumanThrottle(1000), _cfg(), max_relogins=2)
    monkeypatch.setattr("okpos_cli.client.login", lambda cfg, th: _FakeSession(handler))
    client.relogin()
    client.relogin()
    assert client.can_relogin is False
    with pytest.raises(SessionExpired):
        client.relogin()
