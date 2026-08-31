"""Shared physical HTTP policy: pacing, redirects, access blocks, and busy back-off."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from .safety import AccessBlocked, ServerBusy, UnsafeRedirect
from .throttle import HumanThrottle

BUSY_STATUSES = frozenset({429, 503})
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
MAX_BUSY_RETRIES = 3
MAX_REDIRECTS = 10
ACCESS_BLOCKED_STATUSES = frozenset({401, 403})


def parse_retry_after(
    value: str | None, *, now: datetime | None = None
) -> float | None:
    """Seconds to wait per a Retry-After header, if it gives a usable delay."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - (now or datetime.now(UTC))).total_seconds()
    return max(0.0, min(seconds, BACKOFF_MAX_SECONDS))


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme.lower(), (url.host or "").lower(), url.port


def _paced_redirect_chain(
    client: httpx.Client,
    throttle: HumanThrottle,
    method: str,
    url: str | httpx.URL,
    *,
    content: bytes | None,
    headers: dict[str, str] | None,
    params: dict[str, str] | None,
    max_redirects: int,
) -> httpx.Response:
    """Send one attempt, pacing every same-origin redirect hop."""
    current_method = method.upper()
    current_url: str | httpx.URL = url
    current_content = content
    current_headers = dict(headers or {})
    current_params = params

    for followed in range(max_redirects + 1):
        response = throttle.run_request(
            lambda request_method=current_method,
            request_url=current_url,
            request_content=current_content,
            request_headers=current_headers,
            request_params=current_params: client.request(
                request_method,
                request_url,
                content=request_content,
                headers=request_headers,
                params=request_params,
                follow_redirects=False,
            )
        )
        if response.status_code in ACCESS_BLOCKED_STATUSES:
            status = response.status_code
            response_url = response.url
            response.close()
            raise AccessBlocked(f"{response_url}: server returned HTTP {status}")
        if not response.has_redirect_location:
            return response
        if followed >= max_redirects:
            response.close()
            raise UnsafeRedirect(f"redirect limit exceeded ({max_redirects} hops)")

        redirect_url = response.url.join(response.headers["Location"])
        if _origin(response.url) != _origin(redirect_url):
            response_url = response.url
            response.close()
            raise UnsafeRedirect(
                f"cross-origin redirect blocked: {response_url} -> {redirect_url}"
            )

        status = response.status_code
        previous_url = response.url
        response.close()
        switch_to_get = (status == 303 and current_method != "HEAD") or (
            status in {301, 302} and current_method == "POST"
        )
        if switch_to_get:
            current_method = "GET"
            current_content = None
            current_headers.pop("Content-Type", None)
        current_url = redirect_url
        current_params = None
        current_headers["Referer"] = str(previous_url)

    raise AssertionError("unreachable")


def paced_request(
    client: httpx.Client,
    throttle: HumanThrottle,
    method: str,
    url: str | httpx.URL,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    max_redirects: int = MAX_REDIRECTS,
    on_busy: Callable[[], None] | None = None,
) -> httpx.Response:
    """Apply the complete shared policy to one logical HTTP request.

    Each physical redirect and busy retry consumes its own pacing slot and
    request-budget unit. Busy retries restart from the original URL so no
    redirect hop can silently escape accounting.
    """
    if max_redirects < 0:
        raise ValueError("max_redirects must not be negative")
    for attempt in range(MAX_BUSY_RETRIES + 1):
        response = _paced_redirect_chain(
            client,
            throttle,
            method,
            url,
            content=content,
            headers=headers,
            params=params,
            max_redirects=max_redirects,
        )
        if response.status_code not in BUSY_STATUSES:
            return response
        if attempt >= MAX_BUSY_RETRIES:
            status = response.status_code
            response.close()
            raise ServerBusy(
                f"{url}: server returned {status} after {MAX_BUSY_RETRIES} back-offs"
            )
        wait = parse_retry_after(response.headers.get("Retry-After"))
        response.close()
        if wait is None:
            wait = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS)
        if on_busy is not None:
            on_busy()
        time.sleep(wait)

    raise AssertionError("unreachable")
