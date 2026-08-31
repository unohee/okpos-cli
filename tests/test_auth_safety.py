"""Authentication and redirect requests obey the same safety envelope."""

import httpx
import pytest

from okpos_cli.auth import login, paced_request
from okpos_cli.catalog import fetch_catalog
from okpos_cli.config import Config
from okpos_cli.safety import (
    AccessBlocked,
    AuthenticationUnavailable,
    RequestBudgetExceeded,
    RequiredResourceUnavailable,
    ServerBusy,
    UnsafeRedirect,
)
from okpos_cli.throttle import HumanThrottle


def _throttle(max_requests: int) -> HumanThrottle:
    return HumanThrottle(
        1_000_000,
        jitter_median=1e-9,
        pause_probability=0,
        max_requests=max_requests,
    )


def test_each_redirect_hop_is_paced_and_counted():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, text="ok")

    client = httpx.Client(
        base_url="https://x.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    throttle = _throttle(max_requests=2)

    response = paced_request(client, throttle, "GET", "/start")

    assert response.text == "ok"
    assert paths == ["/start", "/final"]
    assert throttle.request_count == len(paths) == 2


def test_redirect_cannot_exceed_remaining_request_budget():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/final"})

    client = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(handler)
    )
    throttle = _throttle(max_requests=1)

    with pytest.raises(RequestBudgetExceeded, match="1/1"):
        paced_request(client, throttle, "GET", "/start")

    assert paths == ["/start"]
    assert throttle.request_count == len(paths) == 1


def test_cross_origin_redirect_is_blocked_before_sending_credentials():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(307, headers={"Location": "https://evil.test/collect"})

    client = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(handler)
    )
    throttle = _throttle(max_requests=2)

    with pytest.raises(UnsafeRedirect, match="cross-origin"):
        paced_request(client, throttle, "POST", "/login", content=b"secret")

    assert paths == ["/login"]
    assert throttle.request_count == 1


@pytest.mark.parametrize("status", [401, 403])
def test_catalog_access_block_is_a_safety_stop(status: int):
    client = httpx.Client(
        base_url="https://x.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, text="blocked")
        ),
    )

    class _Session:
        def __init__(self) -> None:
            self.client = client

    with pytest.raises(AccessBlocked, match=f"HTTP {status}"):
        fetch_catalog(_Session(), _throttle(max_requests=1))  # type: ignore[arg-type]


def test_catalog_honours_retry_after_before_succeeding(monkeypatch):
    calls = 0
    slept: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(
            200,
            text='var AL = [{"PGM_CD":"P1","PGM_FILE_NM":"/x.jsp"}];',
        )

    client = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(handler)
    )

    class _Session:
        def __init__(self) -> None:
            self.client = client

    monkeypatch.setattr("okpos_cli.transport.time.sleep", slept.append)
    throttle = _throttle(max_requests=2)

    programs = fetch_catalog(_Session(), throttle)  # type: ignore[arg-type]

    assert [program.code for program in programs] == ["P1"]
    assert calls == throttle.request_count == 2
    assert slept == [3.0]


def test_catalog_busy_retries_are_bounded(monkeypatch):
    calls = 0
    slept: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="busy")

    client = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(handler)
    )

    class _Session:
        def __init__(self) -> None:
            self.client = client

    monkeypatch.setattr("okpos_cli.transport.time.sleep", slept.append)
    throttle = _throttle(max_requests=4)

    with pytest.raises(ServerBusy, match="after 3 back-offs"):
        fetch_catalog(_Session(), throttle)  # type: ignore[arg-type]

    assert calls == throttle.request_count == 4
    assert slept == [2.0, 4.0, 8.0]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="broken"),
        httpx.Response(200, text="var AL = [];"),
        httpx.Response(200, text="no menu here"),
    ],
)
def test_invalid_required_catalog_is_a_safety_stop(response: httpx.Response):
    client = httpx.Client(
        base_url="https://x.test",
        transport=httpx.MockTransport(lambda _request: response),
    )

    class _Session:
        def __init__(self) -> None:
            self.client = client

    with pytest.raises(RequiredResourceUnavailable):
        fetch_catalog(_Session(), _throttle(max_requests=1))  # type: ignore[arg-type]


def test_catalog_transport_failure_is_a_safety_stop():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.Client(
        base_url="https://x.test", transport=httpx.MockTransport(handler)
    )

    class _Session:
        def __init__(self) -> None:
            self.client = client

    with pytest.raises(RequiredResourceUnavailable, match="ConnectError"):
        fetch_catalog(_Session(), _throttle(max_requests=1))  # type: ignore[arg-type]


def _config() -> Config:
    return Config(
        user_id="u",
        password="p",
        base_url="https://x.test",
        login_path="/login/login_form.jsp",
        pg_dsn=None,
        max_rps=1_000_000,
    )


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, AccessBlocked), (403, AccessBlocked), (500, AuthenticationUnavailable)],
)
def test_login_http_failure_is_a_safety_stop(monkeypatch, status, error_type):
    real_client = httpx.Client

    def client_factory(**kwargs):
        return real_client(
            base_url=kwargs["base_url"],
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(status, text="blocked")
            ),
            follow_redirects=kwargs["follow_redirects"],
        )

    monkeypatch.setattr("okpos_cli.auth.httpx.Client", client_factory)

    with pytest.raises(error_type):
        login(_config(), _throttle(max_requests=4))


def test_login_transport_failure_is_a_safety_stop(monkeypatch):
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    def client_factory(**kwargs):
        return real_client(
            base_url=kwargs["base_url"],
            transport=httpx.MockTransport(handler),
            follow_redirects=kwargs["follow_redirects"],
        )

    monkeypatch.setattr("okpos_cli.auth.httpx.Client", client_factory)

    with pytest.raises(AuthenticationUnavailable, match="ConnectError"):
        login(_config(), _throttle(max_requests=4))
