"""Runtime configuration sourced from the environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

DEFAULT_LOGIN_URL = "https://okasp.okpos.co.kr/login/login_form.jsp"


@dataclass(frozen=True)
class Config:
    user_id: str
    password: str
    base_url: str
    login_path: str
    pg_dsn: str | None
    max_rps: float

    @property
    def has_db(self) -> bool:
        return bool(self.pg_dsn)


def load_config(env_file: Path | None = None) -> Config:
    """Read credentials and tuning knobs, preferring an explicit .env file."""
    load_dotenv(env_file, override=False) if env_file else load_dotenv(override=False)

    user_id = os.getenv("OKPOS_ID", "").strip()
    password = os.getenv("OKPOS_PW", "").strip()
    if not user_id or not password:
        raise SystemExit(
            "OKPOS_ID / OKPOS_PW are not set. Copy .env.example to .env and fill them in."
        )

    raw_url = os.getenv("OKPOS_URL", DEFAULT_LOGIN_URL).strip() or DEFAULT_LOGIN_URL
    parts = urlsplit(raw_url)
    if not parts.scheme or not parts.netloc:
        raise SystemExit(f"OKPOS_URL is not a valid absolute URL: {raw_url!r}")

    return Config(
        user_id=user_id,
        password=password,
        base_url=f"{parts.scheme}://{parts.netloc}",
        login_path=parts.path or "/login/login_form.jsp",
        pg_dsn=(os.getenv("OKPOS_PG_DSN") or "").strip() or None,
        max_rps=float(os.getenv("OKPOS_MAX_RPS", "15")),
    )
