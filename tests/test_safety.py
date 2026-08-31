"""Fail-closed guards for long-running crawls."""

from datetime import date

import httpx
import pytest

from okpos_cli.catalog import Program
from okpos_cli.client import OkposApiError, OkposClient
from okpos_cli.safety import (
    AccessBlocked,
    CircuitOpen,
    RequestBudgetExceeded,
    SafetyStop,
    ServerBusy,
)
from okpos_cli.scraper import Target, resolve_targets, scrape
from okpos_cli.screen import ScreenSpec
from okpos_cli.throttle import HumanThrottle

OK_JSON = '{"Data":[],"Result":{"Message":"조회완료","Code":0}}'


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


def _program(code: str = "P1") -> Program:
    return Program(code, f"program-{code}", f"/sale/{code}.jsp", "sale", "daily")


def _spec(code: str = "P1", *, sheets: int = 2) -> ScreenSpec:
    return ScreenSpec(
        path=f"/sale/{code}.jsp",
        controller=f"sale.{code}",
        fields={"S_CONTROLLER": f"sale.{code}"},
        columns=["A"],
        sheet_count=sheets,
    )


class _AlwaysFailClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def search(self, *_args, **_kwargs):
        self.calls += 1
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        ServerBusy("busy"),
        CircuitOpen("circuit"),
        RequestBudgetExceeded("budget"),
    ],
)
def test_safety_stop_aborts_the_whole_crawl_on_first_failure(exc: SafetyStop):
    client = _AlwaysFailClient(exc)
    targets = [Target(_program("P1"), _spec("P1")), Target(_program("P2"), _spec("P2"))]
    days = [date(2026, 8, day) for day in range(1, 4)]

    with pytest.raises(type(exc), match=str(exc)):
        scrape(client, targets, days)  # type: ignore[arg-type]

    assert client.calls == 1


def test_deterministic_screen_error_remains_isolated():
    client = _AlwaysFailClient(OkposApiError(-1, "bad screen"))
    targets = [Target(_program("P1"), _spec("P1")), Target(_program("P2"), _spec("P2"))]
    days = [date(2026, 8, day) for day in range(1, 4)]

    stats = scrape(client, targets, days)  # type: ignore[arg-type]

    assert client.calls == 12
    assert len(stats.failures) == 12


def test_target_resolution_propagates_safety_stop():
    class _BlockedClient:
        def get_screen(self, *_args, **_kwargs):
            raise AccessBlocked("forbidden")

    with pytest.raises(AccessBlocked, match="forbidden"):
        resolve_targets(_BlockedClient(), [_program()])  # type: ignore[arg-type]


def _client(handler, *, max_requests: int | None = None) -> OkposClient:
    throttle = HumanThrottle(
        1_000_000,
        jitter_median=1e-9,
        pause_probability=0,
        max_requests=max_requests,
    )
    return OkposClient(_FakeSession(handler), throttle)


def test_transport_failures_open_circuit_on_third_request():
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    client = _client(handler)
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            client.search(_spec(sheets=1))

    with pytest.raises(CircuitOpen, match="3 consecutive failures"):
        client.search(_spec(sheets=1))


def test_success_resets_consecutive_infrastructure_failures():
    outcomes = iter(["error", "error", "ok", "error", "error", "error"])

    def handler(request):
        if next(outcomes) == "error":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, text=OK_JSON)

    client = _client(handler)
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            client.search(_spec(sheets=1))
    assert client.search(_spec(sheets=1)).ok
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            client.search(_spec(sheets=1))
    with pytest.raises(CircuitOpen):
        client.search(_spec(sheets=1))


def test_deterministic_http_error_resets_infrastructure_failures():
    outcomes = iter(["error", "error", "404", "error", "error", "error"])

    def handler(request):
        outcome = next(outcomes)
        if outcome == "error":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(404, text="missing")

    client = _client(handler)
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            client.search(_spec(sheets=1))
    with pytest.raises(httpx.HTTPStatusError):
        client.search(_spec(sheets=1))
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            client.search(_spec(sheets=1))
    with pytest.raises(CircuitOpen):
        client.search(_spec(sheets=1))


def test_repeated_server_errors_open_circuit():
    client = _client(lambda _request: httpx.Response(500, text="boom"))

    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            client.search(_spec(sheets=1))
    with pytest.raises(CircuitOpen, match="HTTP 500"):
        client.search(_spec(sheets=1))


@pytest.mark.parametrize("status", [401, 403])
def test_access_block_is_immediately_fatal(status: int):
    client = _client(lambda _request: httpx.Response(status, text="blocked"))

    with pytest.raises(AccessBlocked, match=f"HTTP {status}"):
        client.search(_spec(sheets=1))


def test_repeated_non_json_responses_open_circuit():
    client = _client(lambda _request: httpx.Response(200, text="<html>broken</html>"))

    for _ in range(2):
        with pytest.raises(OkposApiError, match="non-JSON"):
            client.search(_spec(sheets=1))
    with pytest.raises(CircuitOpen, match="non-JSON search response"):
        client.search(_spec(sheets=1))


def test_repeated_invalid_utf8_json_responses_open_circuit():
    client = _client(
        lambda _request: httpx.Response(
            200,
            content=b"\xff",
            headers={"Content-Type": "application/json"},
        )
    )

    for _ in range(2):
        with pytest.raises(OkposApiError, match="non-JSON"):
            client.search(_spec(sheets=1))
    with pytest.raises(CircuitOpen, match="non-JSON search response"):
        client.search(_spec(sheets=1))


def test_repeated_invalid_utf8_data_json_responses_open_circuit():
    client = _client(
        lambda _request: httpx.Response(
            200,
            content=b"\xff",
            headers={"Content-Type": "application/json"},
        )
    )

    for _ in range(2):
        with pytest.raises(OkposApiError, match="non-JSON"):
            client.data_json("sp", "params", "A")
    with pytest.raises(CircuitOpen, match="non-JSON DataJson response"):
        client.data_json("sp", "params", "A")


def test_repeated_malformed_json_responses_open_circuit():
    client = _client(lambda _request: httpx.Response(200, json=["not", "an", "object"]))

    for _ in range(2):
        with pytest.raises(OkposApiError, match="malformed JSON"):
            client.search(_spec(sheets=1))
    with pytest.raises(CircuitOpen, match="malformed JSON search response"):
        client.search(_spec(sheets=1))


def test_success_without_data_opens_circuit_instead_of_persisting_empty_result():
    client = _client(
        lambda _request: httpx.Response(
            200,
            json={"Result": {"Code": 0, "Message": "ok"}},
        )
    )

    for _ in range(2):
        with pytest.raises(OkposApiError, match="Data is missing"):
            client.search(_spec(sheets=1))
    with pytest.raises(CircuitOpen, match="malformed JSON search response"):
        client.search(_spec(sheets=1))


def test_data_json_success_without_data_opens_circuit():
    client = _client(
        lambda _request: httpx.Response(
            200,
            json={"Result": {"Code": 0, "Message": "ok"}},
        )
    )

    for _ in range(2):
        with pytest.raises(OkposApiError, match="Data is missing"):
            client.data_json("sp", "params", "A")
    with pytest.raises(CircuitOpen, match="malformed JSON DataJson response"):
        client.data_json("sp", "params", "A")


def test_null_data_is_not_invented_as_an_empty_success():
    client = _client(
        lambda _request: httpx.Response(
            200,
            json={"Data": None, "Result": {"Code": 0, "Message": "ok"}},
        )
    )

    with pytest.raises(OkposApiError, match="Data is not a list"):
        client.search(_spec(sheets=1))


def test_business_error_may_omit_data_without_counting_as_infrastructure_failure():
    client = _client(
        lambda _request: httpx.Response(
            200,
            json={"Result": {"Code": -9, "Message": "bad screen"}},
        )
    )

    for _ in range(5):
        result = client.search(_spec(sheets=1))
        assert result.code == -9

    assert client.consecutive_infra_failures == 0


def test_request_budget_counts_retries_and_stops_before_next_attempt(monkeypatch):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="busy")

    monkeypatch.setattr("okpos_cli.transport.time.sleep", lambda _seconds: None)
    client = _client(handler, max_requests=2)

    with pytest.raises(RequestBudgetExceeded, match="2/2"):
        client.search(_spec(sheets=1))

    assert calls == 2
    assert client.throttle.request_count == 2
