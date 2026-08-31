"""Data access against the OKPOS internal JSON endpoints.

The IBSheet grids are backed by a servlet mapped to `*SheetAction`, which
answers JSON directly:

    POST <screen_dir>/ddd.htmlSheetAction
      <TokenKey>=<TokenVal>, S_CONTROLLER, S_METHOD=search,
      SHEETSEQ, S_SAVENAME, S_ORDERBY, + the screen's own form fields
    -> {"Data": [...], "Result": {"Message": "조회완료", "Code": 0}}

So the HTML is read once per screen to learn the parameter names, and every
actual row afterwards arrives as JSON.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import FORM_CONTENT_TYPE, Session, encode_form, login
from .config import Config
from .screen import ScreenSpec, parse_screen
from .throttle import HumanThrottle

SHEET_ACTION = "ddd.htmlSheetAction"
DATA_JSON = "/common/jsp/ajax/DataJson.jsp"

# Statuses that mean "slow down" rather than "this request was wrong".
# Not observed on this server in ~1,500 requests (it is plain Apache with no
# rate-limit headers), but honouring them costs nothing if it ever starts.
BUSY_STATUSES = frozenset({429, 503})
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
MAX_BUSY_RETRIES = 3


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait per a Retry-After header, if it gives a usable delay."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        # The HTTP-date form is allowed but this server sends no such header at
        # all; treating it as unknown lets the caller fall back to backoff.
        return None
    return max(0.0, min(seconds, BACKOFF_MAX_SECONDS))


# When the session lapses the server answers with the login page instead of
# JSON, so an expired session looks like a parse failure until the body is read.
_SESSION_LOST_RE = re.compile(r"login_form\.jsp|loginForm|/login/login_check", re.I)


def looks_like_login_page(text: str) -> bool:
    """Whether a response body is the login page rather than real content."""
    return bool(_SESSION_LOST_RE.search(text[:4000]))


class OkposApiError(RuntimeError):
    """The endpoint answered with a non-zero Result.Code."""

    def __init__(self, code: int, message: str, controller: str = "") -> None:
        super().__init__(f"[{controller or '?'}] code={code}: {message}")
        self.code = code
        self.message = message


@dataclass
class SheetResult:
    rows: list[dict[str, Any]]
    code: int
    message: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class SessionExpired(RuntimeError):
    """The server answered with the login page; the session is gone."""


class ServerBusy(RuntimeError):
    """The server asked us to back off and kept doing so."""


class OkposClient:
    """Thin, paced wrapper over the two JSON endpoints.

    Given a `config`, the client re-logins once when it detects an expired
    session and retries the call, so a long crawl is not silently halved.
    """

    def __init__(
        self,
        session: Session,
        throttle: HumanThrottle,
        config: Config | None = None,
        max_relogins: int = 3,
    ) -> None:
        self.session = session
        self.throttle = throttle
        self.config = config
        self.max_relogins = max_relogins
        self.relogin_count = 0
        self.busy_events = 0
        self._screens: dict[str, ScreenSpec] = {}
        # How each screen was opened. The shop tree, for one, parses into a
        # different screen without its query params, so a re-login has to
        # replay the original request rather than a bare GET.
        self._screen_params: dict[str, dict[str, str] | None] = {}

    # -- session recovery ----------------------------------------------

    @property
    def can_relogin(self) -> bool:
        return self.config is not None and self.relogin_count < self.max_relogins

    def relogin(self) -> None:
        """Re-authenticate and drop state that belongs to the dead session.

        Cached screen specs carry that session's CSRF field, so keeping them
        would send a stale token and fail in a way that looks unrelated.
        """
        if self.config is None:
            raise SessionExpired("session expired and no config was given to re-login")
        if self.relogin_count >= self.max_relogins:
            raise SessionExpired(
                f"session expired again after {self.relogin_count} re-logins; giving up"
            )
        self.relogin_count += 1
        try:
            self.session.close()
        except Exception:  # noqa: BLE001 - the old session is being discarded anyway
            pass
        self.session = login(self.config, self.throttle)
        # Specs carry the dead session's CSRF field; the params do not.
        self._screens.clear()

    # -- low level -----------------------------------------------------

    def _send(self, build: Callable[[], httpx.Response], what: str) -> httpx.Response:
        """Issue a request, backing off while the server says it is busy."""
        for attempt in range(MAX_BUSY_RETRIES + 1):
            self.throttle.wait()
            resp = build()
            if resp.status_code not in BUSY_STATUSES:
                return resp
            if attempt == MAX_BUSY_RETRIES:
                raise ServerBusy(
                    f"{what}: server returned {resp.status_code} "
                    f"after {MAX_BUSY_RETRIES} back-offs"
                )
            wait = parse_retry_after(resp.headers.get("Retry-After"))
            if wait is None:
                wait = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS)
            self.busy_events += 1
            time.sleep(wait)
        raise AssertionError("unreachable")

    def _post(self, path: str, data: dict[str, str], referer: str) -> httpx.Response:
        return self._send(
            lambda: self.session.client.post(
                path,
                content=encode_form(data),
                headers={"Referer": str(self.session.client.base_url) + referer,
                         "Content-Type": FORM_CONTENT_TYPE},
            ),
            path,
        )

    def get_screen(
        self, path: str, params: dict[str, str] | None = None, *, retry: bool = True
    ) -> ScreenSpec:
        """Fetch and cache a screen's query spec."""
        if path in self._screens:
            return self._screens[path]
        resp = self._send(
            lambda: self.session.client.get(
                path,
                params=params or None,
                headers={
                    "Referer": str(self.session.client.base_url) + "/login/top_frame.jsp"
                },
            ),
            path,
        )
        resp.raise_for_status()
        # A screen never renders the login form, so seeing it means the session died.
        if looks_like_login_page(resp.text) and "S_CONTROLLER" not in resp.text:
            if not (retry and self.can_relogin):
                raise SessionExpired(f"session expired while loading {path}")
            self.relogin()
            return self.get_screen(path, params, retry=False)
        spec = parse_screen(path, resp.text)
        self._screens[path] = spec
        self._screen_params[path] = params
        return spec

    # -- data ----------------------------------------------------------

    def search(
        self,
        spec: ScreenSpec,
        sheet_seq: int = 1,
        overrides: dict[str, str] | None = None,
        *,
        retry: bool = True,
    ) -> SheetResult:
        """Run one search against one sheet of one screen."""
        if not spec.queryable:
            raise OkposApiError(-98, "screen exposes no queryable form", spec.path)

        payload: dict[str, str] = {**self.session.csrf, **spec.fields}
        payload.update(
            {
                "S_METHOD": "search",
                "SHEETSEQ": str(sheet_seq),
                "S_SAVENAME": "|".join(spec.columns),
                "S_ORDERBY": "",
            }
        )
        if overrides:
            payload.update(overrides)

        directory = spec.path.rsplit("/", 1)[0]
        resp = self._post(f"{directory}/{SHEET_ACTION}", payload, spec.path)
        resp.raise_for_status()
        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            if looks_like_login_page(resp.text):
                if not (retry and self.can_relogin):
                    raise SessionExpired(
                        f"session expired during {spec.controller}#{sheet_seq}"
                    ) from exc
                self.relogin()
                # The cache was cleared, so re-read the screen for a fresh token,
                # replaying the params it was originally opened with.
                fresh = self.get_screen(
                    spec.path, self._screen_params.get(spec.path), retry=False
                )
                return self.search(fresh, sheet_seq, overrides, retry=False)
            raise OkposApiError(-97, f"non-JSON response: {exc}", spec.controller) from exc

        result = body.get("Result") or {}
        return SheetResult(
            rows=body.get("Data") or [],
            code=int(result.get("Code", -1)),
            message=result.get("Message", ""),
        )

    def data_json(
        self, sp_info: str, sp_params: str, save_name: str
    ) -> list[dict[str, Any]]:
        """Query the auxiliary code/combo endpoint."""
        resp = self._post(
            DATA_JSON,
            {"sp_info": sp_info, "sp_params": sp_params, "strSaveName": save_name},
            "/login/top_frame.jsp",
        )
        resp.raise_for_status()
        body = resp.json()
        if int((body.get("Result") or {}).get("Code", -1)) != 0:
            raise OkposApiError(
                int((body.get("Result") or {}).get("Code", -1)),
                (body.get("Result") or {}).get("Message", ""),
            )
        return body.get("Data") or []
