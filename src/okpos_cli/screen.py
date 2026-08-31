"""Screen introspection: turn a JSP screen into a callable query spec.

Screens come in two shapes:

* **direct**  - carries `<form id='form1'>`; its inputs are the query parameters
  and the IBSheet `SaveName:` entries are the columns to request.
* **tabbed**  - a shell containing `myTab1LoadForm`; the real screens are the
  JSP paths passed to `IBS_InitTab`, each of which is itself a direct screen.

Attribute quoting is inconsistent across screens, so every accessor accepts
both single and double quotes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
# Attribute-position match: a name like "unchecked_box" must not read as checked.
_CHECKED_RE = re.compile(r"\schecked\b(?!\s*=\s*['\"]?(?:false|0)['\"]?)", re.I)
_SELECT_RE = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.I | re.S)
_OPTION_RE = re.compile(r"<option[^>]*value\s*=\s*['\"]([^'\"]*)['\"]", re.I)
_SAVENAME_RE = re.compile(r'SaveName\s*:\s*"([^"]+)"')
_SHEET_RE = re.compile(r"\bmySheet(\d+)\b")
_TAB_URL_RE = re.compile(r'["\'](/[\w/]+\.jsp)["\']')
_TABBED_RE = re.compile(r"myTab\d*LoadForm")


def attr(tag: str, name: str) -> str | None:
    """Read an HTML attribute regardless of quote style."""
    m = re.search(rf"\b{name}\s*=\s*'([^']*)'", tag, re.I) or re.search(
        rf'\b{name}\s*=\s*"([^"]*)"', tag, re.I
    )
    return m.group(1) if m else None


@dataclass
class ScreenSpec:
    """Everything needed to issue a search against one screen."""

    path: str
    controller: str
    fields: dict[str, str] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    sheet_count: int = 1
    tab_children: list[str] = field(default_factory=list)

    @property
    def is_tabbed(self) -> bool:
        return bool(self.tab_children)

    @property
    def queryable(self) -> bool:
        return bool(self.controller and self.columns)

    @property
    def needs_shop(self) -> bool:
        return "ss_SHOP_CD" in self.fields

    @property
    def date_fields(self) -> list[str]:
        """Field names that look like a date filter (`date1`, `ss_FR_DT`, ...)."""
        return [
            k
            for k, v in self.fields.items()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v or "")
            or re.search(r"(^|_)(DT|DATE)\d*$|^date\d*$", k, re.I)
        ]


def _form_segment(html: str) -> str:
    for opener in ("<form id='form1'", '<form id="form1"'):
        start = html.find(opener)
        if start >= 0:
            end = html.find("</form>", start)
            return html[start : end if end > 0 else len(html)]
    return ""


def parse_screen(path: str, html: str) -> ScreenSpec:
    """Extract the query spec from a screen's HTML."""
    if _TABBED_RE.search(html) and "S_CONTROLLER" not in html:
        base_dir = path.rsplit("/", 1)[0]
        children = [
            u
            for u in dict.fromkeys(_TAB_URL_RE.findall(html))
            if u != path and u.rsplit("/", 1)[0] == base_dir
        ]
        return ScreenSpec(path=path, controller="", tab_children=children)

    seg = _form_segment(html)
    fields: dict[str, str] = {}
    for m in _INPUT_RE.finditer(seg):
        tag = m.group(0)
        name = attr(tag, "name")
        if not name:
            continue
        kind = (attr(tag, "type") or "text").lower()
        if kind in ("checkbox", "radio") and not _CHECKED_RE.search(tag):
            continue
        fields[name] = attr(tag, "value") or ""
    for m in _SELECT_RE.finditer(seg):
        name = attr("<x" + m.group(1) + ">", "name")
        if not name:
            continue
        opt = _OPTION_RE.search(m.group(2))
        fields[name] = opt.group(1) if opt else ""

    sheets = {int(n) for n in _SHEET_RE.findall(html)}
    return ScreenSpec(
        path=path,
        controller=fields.get("S_CONTROLLER", ""),
        fields=fields,
        columns=list(dict.fromkeys(_SAVENAME_RE.findall(html))),
        sheet_count=max(sheets) if sheets else 1,
    )
