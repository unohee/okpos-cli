"""Program catalogue: the menu the server itself publishes.

`/login/menuv.jsp` renders the whole menu as a `var AL = [...]` JSON literal,
so the crawl target list is read at runtime instead of being hard-coded. New
menus the vendor adds are picked up automatically.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .auth import Session
from .throttle import HumanThrottle

_AL_RE = re.compile(r"var\s+AL\s*=\s*(\[.*?\]);", re.S)


@dataclass(frozen=True)
class Program:
    """One entry of the OKPOS program menu."""

    code: str
    name: str
    path: str
    l_class: str
    m_class: str

    @property
    def full_name(self) -> str:
        return f"{self.l_class} > {self.m_class} > {self.name}".replace(" >  > ", " > ")

    @property
    def screen_path(self) -> str:
        """Path without the query string, for fetching the screen HTML."""
        return self.path.split("?", 1)[0]

    @property
    def query_params(self) -> dict[str, str]:
        """Query string baked into the menu entry (e.g. `?NTC_CD=10`)."""
        if "?" not in self.path:
            return {}
        from urllib.parse import parse_qsl

        return dict(parse_qsl(self.path.split("?", 1)[1]))


def fetch_catalog(session: Session, throttle: HumanThrottle) -> list[Program]:
    """Download and parse the full program menu."""
    throttle.wait()
    resp = session.client.get(
        "/login/menuv.jsp",
        headers={"Referer": str(session.client.base_url) + "/login/top_frame.jsp"},
    )
    resp.raise_for_status()
    m = _AL_RE.search(resp.text)
    if not m:
        raise RuntimeError("Program menu (var AL) not found in /login/menuv.jsp")

    programs = []
    for row in json.loads(m.group(1)):
        path = row.get("PGM_FILE_NM") or ""
        if not path.endswith(".jsp") and ".jsp" not in path:
            continue
        programs.append(
            Program(
                code=row.get("PGM_CD", ""),
                name=row.get("PGM_NM", ""),
                path=path,
                l_class=row.get("PGM_LCLS_NM", ""),
                m_class=row.get("PGM_MCLS_NM", ""),
            )
        )
    return programs
