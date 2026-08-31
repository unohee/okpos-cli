"""Authentication against the OKPOS ASP back office.

Login is a three-hop relay. Each hop mints a fresh CSRF pair whose *name* is
itself a UUID, so the pair has to be re-read from every response rather than
hard-coded:

    GET  /login/login_form.jsp         -> csrf pair #1
    POST /login/login_check.jsp        -> csrf pair #2 (relay form)
    POST /login/login_check_action.jsp -> JSESSIONID established
    GET  /login/top_frame.jsp          -> TokenKey/TokenVal, stable for the session

The token from top_frame.jsp is what every later data call must carry.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

import httpx

from .config import Config
from .throttle import HumanThrottle

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# The pages declare UTF-8 and responses do come back as UTF-8, but the server
# decodes *request* bodies as CP949. Sending UTF-8 makes Korean values come
# home mangled ('전체' -> '??泥?'), so every form body is encoded here instead
# of relying on the HTTP client's default.
FORM_CHARSET = "cp949"
FORM_CONTENT_TYPE = f"application/x-www-form-urlencoded; charset={FORM_CHARSET}"


def encode_form(fields: dict[str, str]) -> bytes:
    """Percent-encode a form body the way this server expects to read it.

    Characters CP949 cannot represent become numeric character references
    rather than being silently replaced, so nothing is lost without a trace.
    """
    return urllib.parse.urlencode(
        fields, encoding=FORM_CHARSET, errors="xmlcharrefreplace"
    ).encode("ascii")

_CSRF_RE = re.compile(
    r"<input[^>]*type=['\"]hidden['\"][^>]*"
    r"name=['\"]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})['\"]"
    r"[^>]*value=['\"]([0-9a-f-]{36})['\"]",
    re.I,
)
_TOKEN_KEY_RE = re.compile(r"id=['\"]TokenKey['\"][^>]*value=['\"]([^'\"]+)['\"]")
_TOKEN_VAL_RE = re.compile(r"id=['\"]TokenVal['\"][^>]*value=['\"]([^'\"]+)['\"]")
_LOGIN_OK_RE = re.compile(r"top_frame\.jsp")


class LoginError(RuntimeError):
    """Raised when the credential relay does not end in an authenticated session."""


@dataclass
class Session:
    """An authenticated OKPOS session plus the CSRF token every call needs."""

    client: httpx.Client
    token_key: str
    token_val: str
    company: str

    @property
    def csrf(self) -> dict[str, str]:
        return {self.token_key: self.token_val}

    def close(self) -> None:
        self.client.close()


def _extract_csrf(html: str) -> tuple[str, str]:
    m = _CSRF_RE.search(html)
    if not m:
        raise LoginError("CSRF token pair not found in response")
    return m.group(1), m.group(2)


def login(cfg: Config, throttle: HumanThrottle | None = None) -> Session:
    """Run the three-hop relay and return an authenticated session."""
    throttle = throttle or HumanThrottle(cfg.max_rps)
    client = httpx.Client(
        base_url=cfg.base_url,
        headers={"User-Agent": UA},
        timeout=60.0,
        follow_redirects=True,
    )

    def _get(path: str, referer: str) -> httpx.Response:
        throttle.wait()
        return client.get(path, headers={"Referer": cfg.base_url + referer})

    def _post(path: str, data: dict[str, str], referer: str) -> httpx.Response:
        throttle.wait()
        return client.post(
            path,
            content=encode_form(data),
            headers={"Referer": cfg.base_url + referer,
                     "Content-Type": FORM_CONTENT_TYPE},
        )

    try:
        # Hop 1 - the login form issues the first CSRF pair.
        form = _get(cfg.login_path, cfg.login_path)
        form.raise_for_status()
        key, val = _extract_csrf(form.text)

        creds = {"AutoFg": "W", "user_id": cfg.user_id, "user_pwd": cfg.password}

        # Hop 2 - returns a relay form carrying a *new* CSRF pair.
        check = _post("/login/login_check.jsp", {key: val, **creds}, cfg.login_path)
        check.raise_for_status()
        key2, val2 = _extract_csrf(check.text)

        # Hop 3 - establishes the session cookie.
        action = _post(
            "/login/login_check_action.jsp",
            {key2: val2, **creds},
            "/login/login_check.jsp",
        )
        action.raise_for_status()
        if not _LOGIN_OK_RE.search(action.text):
            raise LoginError(
                "Login rejected - check OKPOS_ID / OKPOS_PW "
                "(the server did not redirect to top_frame.jsp)"
            )

        # Hop 4 - the session-wide token used by every data call.
        top = _get("/login/top_frame.jsp", "/login/login_check_action.jsp")
        top.raise_for_status()
        tk, tv = _TOKEN_KEY_RE.search(top.text), _TOKEN_VAL_RE.search(top.text)
        if not tk or not tv:
            raise LoginError("Session token (TokenKey/TokenVal) missing from top_frame.jsp")

        title = re.search(r"<title>([^<]*)</title>", top.text)
        return Session(
            client=client,
            token_key=tk.group(1),
            token_val=tv.group(1),
            company=(title.group(1).strip() if title else "unknown"),
        )
    except Exception:
        client.close()
        raise
