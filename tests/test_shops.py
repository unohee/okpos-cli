"""Shop scoping: which screens repeat per shop, and how the tree is read."""

from okpos_cli.scraper import _scopes_for
from okpos_cli.screen import ScreenSpec, parse_screen
from okpos_cli.shops import Shop

SHOP_PICKER_HTML = """
<form id='form1'>
<input type="hidden" name="S_CONTROLLER" value="sale.sale.day_detail010">
<input type='hidden' name='ss_SHOP_CD' value=''>
<input type='text' name='ss_SHOP_NM' value='매장선택'
  onclick="fnCommSearchPopup4('매장','400','500','S','10f5e7f09ac5','','fnSetShopCd','');">
</form>
<script>{SaveName:"PROD_CD"}</script>
"""


def test_shop_token_is_lifted_from_the_picker():
    spec = parse_screen("/sale/sale/day_detail010.jsp", SHOP_PICKER_HTML)
    assert spec.shop_token == "10f5e7f09ac5"
    assert spec.shop_mode == "S"
    assert spec.needs_shop is True


def test_screen_without_picker_has_no_token():
    spec = parse_screen("/x/y.jsp", "<form id='form1'><input name='a' value=''></form>")
    assert spec.shop_token == ""


def _spec(**fields) -> ScreenSpec:
    return ScreenSpec(path="p", controller="c", fields=fields)


def test_shop_agnostic_screen_runs_once_even_with_shops():
    # Repeating it per shop would store identical rows N times.
    assert _scopes_for(_spec(), ["A", "B", "C"], "") == [""]


def test_shop_scoped_screen_repeats_per_shop():
    assert _scopes_for(_spec(ss_SHOP_CD=""), ["A", "B"], "") == ["A", "B"]


def test_single_shop_option_wins_when_no_list():
    assert _scopes_for(_spec(ss_SHOP_CD=""), None, "X") == ["X"]


def test_shop_name_strips_its_code_prefix():
    assert Shop("SHOP001", "[SHOP001] 예시 매장", "직영").clean_name == "예시 매장"
    # A name that does not carry the prefix is left alone.
    assert Shop("V1", "다른 예시 매장", "직영").clean_name == "다른 예시 매장"


def test_runtime_built_column_name_is_completed():
    from okpos_cli.shops import _resolve_columns

    # The tree builds SaveName:"SHOP_"+ss_SEL_GT+"_NM" at runtime; static
    # parsing only sees the prefix, so it must be completed before sending.
    assert _resolve_columns(["LEVEL_FG", "SHOP_", "SHOP_CD"], "TYPE") == [
        "LEVEL_FG", "SHOP_TYPE_NM", "SHOP_CD",
    ]
    assert _resolve_columns(["SHOP_"], "GROUP") == ["SHOP_GROUP_NM"]
    # Missing ss_SEL_GT falls back to the screen's own default.
    assert _resolve_columns(["SHOP_"], "") == ["SHOP_TYPE_NM"]
    # Anything already complete is left alone.
    assert _resolve_columns(["SHOP_CD", "SHOP_NM"], "TYPE") == ["SHOP_CD", "SHOP_NM"]


def test_empty_savename_is_not_treated_as_a_column():
    from okpos_cli.screen import parse_screen

    html = """<form id='form1'><input name='S_CONTROLLER' value='c'></form>
    <script>{SaveName:"A"}, {SaveName:""}, {SaveName:"B"}</script>"""
    assert parse_screen("/x.jsp", html).columns == ["A", "B"]


def test_shop_agnostic_screen_is_done_under_any_previous_scope():
    """Older runs stored shop-agnostic rows under the global --shop code.

    Matching those on shop_cd would re-scrape and duplicate them, because the
    natural key includes shop_cd.
    """
    from datetime import date

    from okpos_cli.scraper import _already_done

    day = date(2026, 8, 25)
    done = {("c", 1, "V9", day)}       # recorded by an older `--shop V9` run
    done_any = {("c", 1, day)}

    assert _already_done(_spec(), 1, "", day, done, done_any) is True


def test_shop_scoped_screen_still_keys_on_the_shop():
    from datetime import date

    from okpos_cli.scraper import _already_done

    day = date(2026, 8, 25)
    done = {("c", 1, "V9", day)}
    done_any = {("c", 1, day)}
    spec = _spec(ss_SHOP_CD="")

    assert _already_done(spec, 1, "V9", day, done, done_any) is True
    # A different shop is genuinely uncollected, even though done_any matches.
    assert _already_done(spec, 1, "W1", day, done, done_any) is False
