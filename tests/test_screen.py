"""Screen parsing: the part that must survive OKPOS markup quirks."""

from okpos_cli.screen import attr, parse_screen

DIRECT_HTML = """
<html><body>
<form id='form1' name='form1' method='post'>
<input type='hidden' id='tok' name='7defdfa1' value='a665846c'>
<input type="hidden" name="S_CONTROLLER" value="sale.sale.day_summery010">
<input type='hidden' name='S_METHOD' value=''>
<input type='text' name='date1' value='2026-08-31'>
<input type='hidden' name='ss_SHOP_CD' value=''>
<input type='checkbox' name='unchecked_box' value='1'>
<input type='checkbox' name='checked_box' value='1' checked>
<select name='ss_POS_NO'><option value='A'>all</option><option value='B'>b</option></select>
</form>
<script>
var mySheet1, mySheet2;
{Header:"x", SaveName:"PROD_CD"}, {Header:"y", SaveName:"SALE_AMT"}
</script>
</body></html>
"""

TABBED_HTML = """
<html><body>
<form id='myTab1LoadForm' name='myTab1LoadForm' target='myTab1PageFrm'></form>
<script>
IBS_InitTab(myTab1, "총괄", "/sale/day/day_total010.jsp");
IBS_InitTab(myTab1, "매장", "/sale/day/day_shop010.jsp");
var self_ref = "/sale/day/day_jump010.jsp";
var other = "/elsewhere/x010.jsp";
</script>
</body></html>
"""


def test_attr_handles_both_quote_styles():
    assert attr("<input name='a'>", "name") == "a"
    assert attr('<input name="b">', "name") == "b"
    assert attr("<input id='c'>", "name") is None


def test_direct_screen_extracts_controller_fields_and_columns():
    spec = parse_screen("/sale/sale/day_summery010.jsp", DIRECT_HTML)
    assert spec.controller == "sale.sale.day_summery010"
    assert spec.columns == ["PROD_CD", "SALE_AMT"]
    assert spec.sheet_count == 2
    assert spec.queryable is True
    assert spec.is_tabbed is False
    # Selects fall back to their first option.
    assert spec.fields["ss_POS_NO"] == "A"
    # Unchecked boxes are not submitted; checked ones are.
    assert "unchecked_box" not in spec.fields
    assert spec.fields["checked_box"] == "1"
    # The UUID-named CSRF field is preserved verbatim.
    assert spec.fields["7defdfa1"] == "a665846c"


def test_direct_screen_detects_date_and_shop_requirements():
    spec = parse_screen("/sale/sale/day_summery010.jsp", DIRECT_HTML)
    assert spec.date_fields == ["date1"]
    assert spec.needs_shop is True


def test_tabbed_screen_lists_sibling_children_only():
    spec = parse_screen("/sale/day/day_jump010.jsp", TABBED_HTML)
    assert spec.is_tabbed is True
    assert spec.tab_children == [
        "/sale/day/day_total010.jsp",
        "/sale/day/day_shop010.jsp",
    ]
    # Its own path and unrelated directories are excluded.
    assert "/sale/day/day_jump010.jsp" not in spec.tab_children
    assert "/elsewhere/x010.jsp" not in spec.tab_children


def test_screen_without_form_is_not_queryable():
    spec = parse_screen("/x/y.jsp", "<html><body>nothing here</body></html>")
    assert spec.queryable is False
