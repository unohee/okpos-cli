"""Config parsing, including the URL split that yields base_url."""

import pytest

from okpos_cli.config import load_config


def test_splits_login_url_into_base_and_path(monkeypatch, tmp_path):
    monkeypatch.setenv("OKPOS_ID", "u")
    monkeypatch.setenv("OKPOS_PW", "p")
    monkeypatch.setenv("OKPOS_URL", "https://okasp.okpos.co.kr/login/login_form.jsp")
    monkeypatch.delenv("OKPOS_PG_DSN", raising=False)
    # Isolate from the repo's own .env, which would otherwise inject a DSN.
    cfg = load_config(env_file=tmp_path / "absent.env")
    assert cfg.base_url == "https://okasp.okpos.co.kr"
    assert cfg.login_path == "/login/login_form.jsp"
    assert cfg.has_db is False
    assert cfg.max_rps == 15


def test_missing_credentials_is_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv("OKPOS_ID", "")
    monkeypatch.setenv("OKPOS_PW", "")
    with pytest.raises(SystemExit):
        load_config(env_file=tmp_path / "absent.env")


def test_rejects_relative_url(monkeypatch, tmp_path):
    monkeypatch.setenv("OKPOS_ID", "u")
    monkeypatch.setenv("OKPOS_PW", "p")
    monkeypatch.setenv("OKPOS_URL", "/login/login_form.jsp")
    with pytest.raises(SystemExit):
        load_config(env_file=tmp_path / "absent.env")
