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
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import FORM_CONTENT_TYPE, Session, encode_form
from .screen import ScreenSpec, parse_screen
from .throttle import HumanThrottle

SHEET_ACTION = "ddd.htmlSheetAction"
DATA_JSON = "/common/jsp/ajax/DataJson.jsp"


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


class OkposClient:
    """Thin, paced wrapper over the two JSON endpoints."""

    def __init__(self, session: Session, throttle: HumanThrottle) -> None:
        self.session = session
        self.throttle = throttle
        self._screens: dict[str, ScreenSpec] = {}

    # -- low level -----------------------------------------------------

    def _post(self, path: str, data: dict[str, str], referer: str) -> httpx.Response:
        self.throttle.wait()
        return self.session.client.post(
            path,
            content=encode_form(data),
            headers={"Referer": str(self.session.client.base_url) + referer,
                     "Content-Type": FORM_CONTENT_TYPE},
        )

    def get_screen(self, path: str, params: dict[str, str] | None = None) -> ScreenSpec:
        """Fetch and cache a screen's query spec."""
        if path in self._screens:
            return self._screens[path]
        self.throttle.wait()
        resp = self.session.client.get(
            path,
            params=params or None,
            headers={"Referer": str(self.session.client.base_url) + "/login/top_frame.jsp"},
        )
        resp.raise_for_status()
        spec = parse_screen(path, resp.text)
        self._screens[path] = spec
        return spec

    # -- data ----------------------------------------------------------

    def search(
        self,
        spec: ScreenSpec,
        sheet_seq: int = 1,
        overrides: dict[str, str] | None = None,
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
