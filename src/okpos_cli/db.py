"""Postgres persistence.

Hybrid schema: stable metadata columns for filtering, plus the untouched row
as JSONB. Screens expose anywhere from 14 to 40 columns and the vendor can
change them, so pinning a physical column per field would mean a migration
every time; the JSONB payload absorbs that while the metadata stays queryable.

`scrape_run` records what has already been collected so a re-run can skip it.
Both tables are keyed so that re-running is idempotent rather than duplicating.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS okpos;

CREATE TABLE IF NOT EXISTS okpos.program (
    program_cd  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL,
    l_class     TEXT NOT NULL DEFAULT '',
    m_class     TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS okpos.record (
    id          BIGSERIAL PRIMARY KEY,
    program_cd  TEXT NOT NULL DEFAULT '',
    controller  TEXT NOT NULL,
    screen_path TEXT NOT NULL,
    sheet_seq   INTEGER NOT NULL,
    shop_cd     TEXT NOT NULL DEFAULT '',
    biz_date    DATE NOT NULL,
    row_no      INTEGER NOT NULL,
    payload     JSONB NOT NULL,
    scraped_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT record_natural_key
        UNIQUE (controller, sheet_seq, shop_cd, biz_date, row_no)
);

CREATE INDEX IF NOT EXISTS record_biz_date_idx   ON okpos.record (biz_date);
CREATE INDEX IF NOT EXISTS record_controller_idx ON okpos.record (controller, biz_date);
CREATE INDEX IF NOT EXISTS record_payload_idx    ON okpos.record USING GIN (payload);

CREATE TABLE IF NOT EXISTS okpos.scrape_run (
    controller  TEXT NOT NULL,
    sheet_seq   INTEGER NOT NULL,
    shop_cd     TEXT NOT NULL DEFAULT '',
    biz_date    DATE NOT NULL,
    screen_path TEXT NOT NULL DEFAULT '',
    row_count   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    message     TEXT NOT NULL DEFAULT '',
    scraped_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (controller, sheet_seq, shop_cd, biz_date)
);
"""


@dataclass
class Store:
    """Connection holder for the OKPOS schema."""

    dsn: str

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            yield conn

    def init_schema(self) -> list[str]:
        """Apply the DDL and return the tables that actually exist afterwards."""
        with self.connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()
            rows = conn.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'okpos' ORDER BY tablename
                """
            ).fetchall()
        return [r["tablename"] for r in rows]

    def save_programs(self, programs) -> int:
        rows = [
            (p.code, p.name, p.path, p.l_class, p.m_class) for p in programs if p.code
        ]
        if not rows:
            return 0
        with self.connect() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO okpos.program (program_cd, name, path, l_class, m_class)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (program_cd) DO UPDATE SET
                    name = EXCLUDED.name, path = EXCLUDED.path,
                    l_class = EXCLUDED.l_class, m_class = EXCLUDED.m_class,
                    updated_at = now()
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def save_rows(
        self,
        *,
        program_cd: str,
        controller: str,
        screen_path: str,
        sheet_seq: int,
        shop_cd: str,
        biz_date: date,
        rows: list[dict[str, Any]],
    ) -> int:
        """Upsert one sheet's rows. Re-running the same key overwrites in place."""
        if not rows:
            return 0
        params = [
            (
                program_cd,
                controller,
                screen_path,
                sheet_seq,
                shop_cd,
                biz_date,
                i,
                json.dumps(r, ensure_ascii=False),
            )
            for i, r in enumerate(rows)
        ]
        with self.connect() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO okpos.record (program_cd, controller, screen_path, sheet_seq,
                                          shop_cd, biz_date, row_no, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT ON CONSTRAINT record_natural_key DO UPDATE SET
                    payload = EXCLUDED.payload,
                    program_cd = EXCLUDED.program_cd,
                    screen_path = EXCLUDED.screen_path,
                    scraped_at = now()
                """,
                params,
            )
            conn.commit()
        return len(params)

    def mark_run(
        self,
        *,
        controller: str,
        sheet_seq: int,
        shop_cd: str,
        biz_date: date,
        screen_path: str,
        row_count: int,
        status: str,
        message: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO okpos.scrape_run (controller, sheet_seq, shop_cd, biz_date,
                                              screen_path, row_count, status, message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (controller, sheet_seq, shop_cd, biz_date) DO UPDATE SET
                    row_count = EXCLUDED.row_count, status = EXCLUDED.status,
                    message = EXCLUDED.message, screen_path = EXCLUDED.screen_path,
                    scraped_at = now()
                """,
                (
                    controller,
                    sheet_seq,
                    shop_cd,
                    biz_date,
                    screen_path,
                    row_count,
                    status,
                    message[:500],
                ),
            )
            conn.commit()

    def completed_keys(self, biz_dates: list[date]) -> set[tuple[str, int, str, date]]:
        """Keys already collected successfully, used to skip work on re-runs."""
        if not biz_dates:
            return set()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT controller, sheet_seq, shop_cd, biz_date
                FROM okpos.scrape_run
                WHERE status = 'ok' AND biz_date = ANY(%s)
                """,
                (biz_dates,),
            ).fetchall()
        return {
            (r["controller"], r["sheet_seq"], r["shop_cd"], r["biz_date"]) for r in rows
        }

    def completed_any_scope(self, biz_dates: list[date]) -> set[tuple[str, int, date]]:
        """Keys done under *any* shop scope.

        Shop-agnostic screens return the same rows whatever `shop_cd` was in
        effect, and versions before shop iteration stored them under the global
        `--shop` code. Matching those on `shop_cd` would re-scrape and duplicate
        them, so they are matched without it.
        """
        if not biz_dates:
            return set()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT controller, sheet_seq, biz_date
                FROM okpos.scrape_run
                WHERE status = 'ok' AND biz_date = ANY(%s)
                """,
                (biz_dates,),
            ).fetchall()
        return {(r["controller"], r["sheet_seq"], r["biz_date"]) for r in rows}

    def fetch_records(
        self,
        *,
        controller: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[sql.Composable] = []
        params: list[Any] = []
        if controller:
            clauses.append(sql.SQL("controller = %s"))
            params.append(controller)
        if date_from:
            clauses.append(sql.SQL("biz_date >= %s"))
            params.append(date_from)
        if date_to:
            clauses.append(sql.SQL("biz_date <= %s"))
            params.append(date_to)
        where = (
            sql.SQL("WHERE {}").format(sql.SQL(" AND ").join(clauses))
            if clauses
            else sql.SQL("")
        )
        query = sql.SQL(
            """
            SELECT controller, screen_path, sheet_seq, shop_cd, biz_date,
                   row_no, payload, scraped_at
            FROM okpos.record {}
            ORDER BY controller, biz_date, sheet_seq, row_no
            """
        ).format(where)
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def controllers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT r.controller, MIN(r.biz_date) AS first_date,
                       MAX(r.biz_date) AS last_date, COUNT(*) AS rows,
                       COALESCE(MAX(p.name), '') AS name
                FROM okpos.record r
                LEFT JOIN okpos.program p ON p.program_cd = r.program_cd
                GROUP BY r.controller ORDER BY r.controller
                """
            ).fetchall()
