"""Form encoding: the server reads request bodies as CP949, not UTF-8."""

import urllib.parse

from okpos_cli.auth import FORM_CHARSET, encode_form


def test_korean_is_encoded_as_cp949_not_utf8():
    body = encode_form({"tree_cols": "전체"})
    # CP949 '전체' is %C0%FC%C3%BC; UTF-8 would be %EC%A0%84%EC%B2%B4.
    assert body == b"tree_cols=%C0%FC%C3%BC"
    assert b"%EC%A0%84" not in body


def test_round_trip_through_the_declared_charset():
    fields = {"a": "전체", "b": "예시 매장"}
    decoded = dict(
        urllib.parse.parse_qsl(encode_form(fields).decode("ascii"), encoding=FORM_CHARSET)
    )
    assert decoded == fields


def test_ascii_is_unaffected():
    assert encode_form({"S_METHOD": "search", "SHEETSEQ": "1"}) == (
        b"S_METHOD=search&SHEETSEQ=1"
    )


def test_characters_outside_cp949_survive_as_references():
    # A silent '?' would lose the value without a trace; a numeric reference
    # at least carries it through.
    body = encode_form({"x": "😀"}).decode("ascii")
    assert "%26%23128512%3B" in body or "&#128512;" in urllib.parse.unquote(body)
