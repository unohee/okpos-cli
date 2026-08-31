"""Back-off on 429/503.

Not observed on this server in ~1,500 requests (plain Apache, no rate-limit
headers), so these tests are the only place the behaviour is exercised.
"""

import httpx
import pytest

from okpos_cli.client import (
    BACKOFF_MAX_SECONDS,
    MAX_BUSY_RETRIES,
    OkposClient,
    ServerBusy,
    parse_retry_after,
)
from okpos_cli.screen import ScreenSpec
from okpos_cli.throttle import HumanThrottle

SCREEN_HTML = """<form id='form1'>
<input type='hidden' name='S_CONTROLLER' value='sale.sale.x010'>
</form><script>{SaveName:"A"}</script>"""
OK_JSON = '{"Data":[{"A":"1"}],"Result":{"Message":"조회완료","Code":0}}'


class _FakeSession:
    def __init__(self, handler):
        self.client = httpx.Client(
            base_url="https://x.test", transport=httpx.MockTransport(handler)
        )
        self.token_key, self.token_val = "sess", "tok"
        self.company = "test"

    @property
    def csrf(self):
        return {self.token_key: self.token_val}

    def close(self):
        self.client.close()


def _spec():
    return ScreenSpec(path="/sale/sale/x010.jsp", controller="sale.sale.x010",
                      fields={"S_CONTROLLER": "sale.sale.x010"}, columns=["A"])


def _client(handler, monkeypatch):
    """Client whose only sleeping is the back-off under test.

    The throttle sleeps too, and both go through the same `time.sleep`, so it
    is silenced here to keep the assertions about back-off alone.
    """
    slept: list[float] = []
    monkeypatch.setattr(HumanThrottle, "wait", lambda self: None)
    monkeypatch.setattr("okpos_cli.client.time.sleep", slept.append)
    return OkposClient(_FakeSession(handler), HumanThrottle(1000)), slept


def test_retry_after_is_parsed_and_capped():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(" 2.5 ") == 2.5
    assert parse_retry_after(None) is None
    # An HTTP-date is legal but unusable here; caller falls back to backoff.
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after("99999") == BACKOFF_MAX_SECONDS
    assert parse_retry_after("-5") == 0.0


def test_429_is_retried_and_then_succeeds(monkeypatch):
    seen = {"n": 0}

    def handler(request):
        if request.url.path.endswith("SheetAction"):
            seen["n"] += 1
            if seen["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, text="busy")
            return httpx.Response(200, text=OK_JSON,
                                  headers={"Content-Type": "text/json"})
        return httpx.Response(200, text=SCREEN_HTML)

    client, slept = _client(handler, monkeypatch)
    result = client.search(_spec(), 1)

    assert result.ok
    assert slept == [3.0]           # honoured the header rather than guessing
    assert client.busy_events == 1


def test_backoff_is_exponential_without_a_header(monkeypatch):
    def handler(request):
        if request.url.path.endswith("SheetAction"):
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, text=SCREEN_HTML)

    client, slept = _client(handler, monkeypatch)
    with pytest.raises(ServerBusy):
        client.search(_spec(), 1)

    assert slept == [2.0, 4.0, 8.0]
    assert len(slept) == MAX_BUSY_RETRIES


def test_giving_up_raises_rather_than_looping(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        if request.url.path.endswith("SheetAction"):
            calls["n"] += 1
            return httpx.Response(429, text="busy")
        return httpx.Response(200, text=SCREEN_HTML)

    client, _ = _client(handler, monkeypatch)
    with pytest.raises(ServerBusy):
        client.search(_spec(), 1)
    # One initial attempt plus the retries, then it stops knocking.
    assert calls["n"] == MAX_BUSY_RETRIES + 1


def test_ordinary_errors_are_not_treated_as_busy(monkeypatch):
    def handler(request):
        if request.url.path.endswith("SheetAction"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=SCREEN_HTML)

    client, slept = _client(handler, monkeypatch)
    with pytest.raises(httpx.HTTPStatusError):
        client.search(_spec(), 1)
    assert slept == []              # a 500 is not a "slow down" signal
