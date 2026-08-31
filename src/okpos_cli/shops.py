"""Shop catalogue.

The shop list lives behind the picker popup, not in the program menu. Opening
`/common/jsp/shop_group_type_tree.jsp` requires an encrypted `TG_INFO` token,
which every screen with a shop filter embeds in its `fnCommSearchPopup4` call.
So any such screen can seed the lookup.

The popup answers through the same `SheetAction` endpoint, but `ss_SEL_GT`
(형태별/그룹별) selects which tree query the server runs. Leaving it empty is
what produces `code=-9 미등록 SQL Index 번호입니다`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import OkposApiError, OkposClient

TREE_PATH = "/common/jsp/shop_group_type_tree.jsp"
# Every row carries `Level`; `LEVEL_FG` appears on shop rows only, so `Level` is
# the field to branch on. 0 = 전체 root, 1 = 직영/가맹 group, 2 = an actual shop.
GROUP_LEVEL = 1
SHOP_LEVEL = 2
# The tree builds one column name at runtime: SaveName:"SHOP_"+ss_SEL_GT+"_NM".
# Static parsing only sees the "SHOP_" prefix, so it is completed here.
_DYNAMIC_COL_PREFIX = "SHOP_"


def _resolve_columns(columns: list[str], sel_gt: str) -> list[str]:
    """Complete the runtime-built column name (`SHOP_` -> `SHOP_TYPE_NM`)."""
    suffix = sel_gt or "TYPE"
    return [
        f"{_DYNAMIC_COL_PREFIX}{suffix}_NM" if c == _DYNAMIC_COL_PREFIX else c
        for c in columns
    ]


@dataclass(frozen=True)
class Shop:
    code: str
    name: str
    group: str

    @property
    def clean_name(self) -> str:
        """Strip the `[CODE] ` prefix the tree prepends to every shop name."""
        prefix = f"[{self.code}] "
        return self.name[len(prefix):] if self.name.startswith(prefix) else self.name


SEED_SCREEN = "/sale/sale/day_detail010.jsp"


def fetch_shops(client: OkposClient, seed_screen: str = SEED_SCREEN) -> list[Shop]:
    """Return every shop the account can see.

    `seed_screen` only supplies the picker token; any screen with a shop filter
    works, and a screen without one raises rather than silently returning [].
    """
    seed = client.get_screen(seed_screen)
    if not seed.shop_token:
        raise OkposApiError(-96, f"{seed_screen} has no shop picker to source a token from")

    spec = client.get_screen(
        TREE_PATH,
        {
            "OpenerFunc": "fnSetShopCd",
            "TG_INFO": seed.shop_token,
            "DWHEREKV": "",
            "AutoSettingValYN": "N",
            "SmFg": seed.shop_mode,
        },
    )
    columns = _resolve_columns(spec.columns, spec.fields.get("ss_SEL_GT", ""))
    result = client.search(
        spec, 1, {"S_SAVENAME": "|".join(columns), "strSaveName": "|".join(columns)}
    )
    if not result.ok:
        raise OkposApiError(result.code, result.message, spec.controller)

    shops, group = [], ""
    for row in result.rows:
        try:
            level = int(row.get("Level", -1))
        except (TypeError, ValueError):
            continue
        if level == GROUP_LEVEL:
            group = row.get("TREE_NM", "")
        elif level == SHOP_LEVEL and row.get("SHOP_CD"):
            shops.append(
                Shop(code=row["SHOP_CD"], name=row.get("SHOP_NM", ""), group=group)
            )
    return shops
