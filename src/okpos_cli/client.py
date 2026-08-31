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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import FORM_CONTENT_TYPE, Session, encode_form, login
from .config import Config
from .safety import CircuitOpen, SafetyStop
from .screen import ScreenSpec, parse_screen
from .throttle import HumanThrottle
from .transport import paced_request

SHEET_ACTION = "ddd.htmlSheetAction"
DATA_JSON = "/common/jsp/ajax/DataJson.jsp"

MAX_CONSECUTIVE_INFRA_FAILURES = 3


# When the session lapses the server answers with the login page instead of
# JSON, so an expired session looks like a parse failure until the body is read.
_SESSION_LOST_RE = re.compile(r"login_form\.jsp|loginForm|/login/login_check", re.I)


def looks_like_login_page(text: str) -> bool:
    """Whether a response body is the login page rather than real content."""
    return bool(_SESSION_LOST_RE.search(text[:4000]))


def _parse_api_envelope(body: Any) -> tuple[int, str, list[dict[str, Any]]]:
    """Validate the shared Result/Data JSON shape without inventing empty success."""
    if not isinstance(body, dict):
        raise TypeError("top-level JSON value is not an object")
    if "Result" not in body or not isinstance(body["Result"], dict):
        raise TypeError("Result is missing or is not an object")
    result = body["Result"]
    if "Code" not in result:
        raise TypeError("Result.Code is missing")
    code = int(result["Code"])
    message = str(result.get("Message") or "")

    # A business error may legitimately omit Data. Its Result.Code is enough
    # to record that one screen as failed without blaming the infrastructure.
    if code != 0:
        return code, message, []
    if "Data" not in body:
        raise TypeError("Data is missing from a successful response")
    rows = body["Data"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TypeError("Data is not a list of objects")
    return code, message, rows


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


class SessionExpired(SafetyStop):
    """The server answered with the login page; the session is gone."""


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
        self.consecutive_infra_failures = 0
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
        try:
            self.session.close()
        except Exception as exc:  # noqa: BLE001 - do not open a second live session
            raise SessionExpired(
                "could not close the expired session safely before re-login: "
                f"{type(exc).__name__}"
            ) from exc
        self.relogin_count += 1
        self.session = login(self.config, self.throttle)
        # Specs carry the dead session's CSRF field; the params do not.
        self._screens.clear()

    # -- low level -----------------------------------------------------

    def _record_infra_failure(self, reason: str) -> None:
        self.consecutive_infra_failures += 1
        if self.consecutive_infra_failures >= MAX_CONSECUTIVE_INFRA_FAILURES:
            raise CircuitOpen(
                f"infrastructure failure circuit opened after "
                f"{self.consecutive_infra_failures} consecutive failures: {reason}"
            )

    def _record_infra_success(self) -> None:
        self.consecutive_infra_failures = 0

    def _record_busy(self) -> None:
        self.busy_events += 1

    def _send(
        self,
        build: Callable[[], httpx.Response],
        *,
        record_success: bool = True,
    ) -> httpx.Response:
        """Classify one response after the shared transport policy has run."""
        try:
            resp = build()
        except httpx.TransportError as exc:
            self._record_infra_failure(type(exc).__name__)
            raise
        if resp.status_code >= 500:
            self._record_infra_failure(f"HTTP {resp.status_code}")
        elif record_success or not 200 <= resp.status_code < 300:
            self._record_infra_success()
        return resp

    def _post(self, path: str, data: dict[str, str], referer: str) -> httpx.Response:
        return self._send(
            lambda: paced_request(
                self.session.client,
                self.throttle,
                "POST",
                path,
                content=encode_form(data),
                headers={
                    "Referer": str(self.session.client.base_url) + referer,
                    "Content-Type": FORM_CONTENT_TYPE,
                },
                on_busy=self._record_busy,
            ),
            # A 200 response is not an infrastructure success until its JSON
            # body has parsed. Otherwise repeated proxy/login garbage would
            # reset the circuit immediately before counting as a failure.
            record_success=False,
        )

    def get_screen(
        self, path: str, params: dict[str, str] | None = None, *, retry: bool = True
    ) -> ScreenSpec:
        """Fetch and cache a screen's query spec."""
        if path in self._screens:
            return self._screens[path]
        resp = self._send(
            lambda: paced_request(
                self.session.client,
                self.throttle,
                "GET",
                path,
                params=params or None,
                headers={
                    "Referer": str(self.session.client.base_url)
                    + "/login/top_frame.jsp"
                },
                on_busy=self._record_busy,
            ),
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
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
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
            self._record_infra_failure("non-JSON search response")
            raise OkposApiError(
                -97, f"non-JSON response: {exc}", spec.controller
            ) from exc

        try:
            code, message, rows = _parse_api_envelope(body)
        except (TypeError, ValueError) as exc:
            self._record_infra_failure("malformed JSON search response")
            raise OkposApiError(
                -97, f"malformed JSON response: {exc}", spec.controller
            ) from exc
        self._record_infra_success()
        return SheetResult(
            rows=rows,
            code=code,
            message=message,
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
        try:
            body = resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._record_infra_failure("non-JSON DataJson response")
            raise OkposApiError(-97, f"non-JSON response: {exc}") from exc
        try:
            code, message, rows = _parse_api_envelope(body)
        except (TypeError, ValueError) as exc:
            self._record_infra_failure("malformed JSON DataJson response")
            raise OkposApiError(-97, f"malformed JSON response: {exc}") from exc
        self._record_infra_success()
        if code != 0:
            raise OkposApiError(code, message)
        return rows
