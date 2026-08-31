"""Command line entry points."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .auth import login
from .catalog import fetch_catalog
from .client import OkposClient
from .config import load_config
from .db import Store
from .export import default_export_name, write_xlsx
from .scraper import date_range, resolve_targets, scrape
from .shops import fetch_shops
from .throttle import HumanThrottle

app = typer.Typer(
    add_completion=False,
    help="Scrape the OKPOS ASP back office into Postgres and xlsx.",
)
console = Console()


def _parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from exc


def summarize_failures(
    failures: list[tuple[str, str]], limit: int = 10
) -> list[tuple[str, str, int]]:
    """Collapse failures that share a cause into one line each.

    A screen that fails once usually fails for every shop and every date, so a
    30-day x 16-shop run would otherwise print the same message hundreds of
    times. Returns (screen, message, count) ordered by count.
    """
    grouped: dict[tuple[str, str], int] = {}
    for name, msg in failures:
        key = (name.split("@")[0], msg[:90])
        grouped[key] = grouped.get(key, 0) + 1
    ranked = sorted(grouped.items(), key=lambda kv: (-kv[1], kv[0][0]))
    return [(name, msg, count) for (name, msg), count in ranked[:limit]]


def summarize_adaptive_throttle(throttle: HumanThrottle) -> str | None:
    """Human-readable latency/adaptive pacing state after a crawl."""
    baseline = throttle.latency_baseline_seconds
    ewma = throttle.latency_ewma_seconds
    if baseline is None or ewma is None:
        return None
    return (
        f"응답시간 기준선 {baseline:.3f}초 · EWMA {ewma:.3f}초 ({ewma / baseline:.1f}×) · "
        f"적응 감속 {throttle.adaptive_events}회 "
        f"(현재 +{throttle.adaptive_delay_seconds:.2f}초, "
        f"최대 +{throttle.max_adaptive_delay_seconds:.2f}초)"
    )


def _connect(seed: int | None = None):
    cfg = load_config()
    throttle = HumanThrottle(cfg.max_rps, seed=seed)
    with console.status("[cyan]OKPOS 로그인 중..."):
        session = login(cfg, throttle)
    console.print(f"[green]OK[/] {session.company}")
    return cfg, throttle, session


def _store_or_exit(cfg) -> Store:
    if not cfg.has_db:
        raise typer.BadParameter("OKPOS_PG_DSN is not set; cannot reach Postgres")
    return Store(cfg.pg_dsn)


@app.command()
def check() -> None:
    """Verify credentials, session and the observed request rate."""
    cfg, throttle, session = _connect()
    programs = fetch_catalog(session, throttle)
    session.close()
    console.print(f"프로그램 카탈로그: [bold]{len(programs)}[/]개")
    console.print(
        f"요청 페이싱: 상한 {cfg.max_rps} RPS / 실측 피크 "
        f"{throttle.observed_peak_rps():.0f} RPS"
    )
    console.print(f"Postgres: {'설정됨' if cfg.has_db else '[yellow]미설정[/]'}")


@app.command("menu")
def menu_cmd(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table"),
    filter_class: str = typer.Option("", "--class", help="Filter by menu class name"),
) -> None:
    """List the program catalogue the server publishes."""
    _cfg, throttle, session = _connect()
    programs = fetch_catalog(session, throttle)
    session.close()
    if filter_class:
        needle = filter_class.lower()
        programs = [
            p for p in programs
            if needle in p.l_class.lower() or needle in p.m_class.lower()
        ]

    if as_json:
        console.print_json(
            json.dumps([p.__dict__ for p in programs], ensure_ascii=False)
        )
        return

    table = Table(title=f"OKPOS 프로그램 {len(programs)}개")
    for col in ("코드", "대분류", "중분류", "프로그램", "경로"):
        table.add_column(col, overflow="fold")
    for p in programs:
        table.add_row(p.code, p.l_class, p.m_class, p.name, p.path)
    console.print(table)


@app.command("shops")
def shops_cmd(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table"),
) -> None:
    """List the shops this account can see."""
    cfg, throttle, session = _connect()
    client = OkposClient(session, throttle, cfg)
    shops = fetch_shops(client)
    client.session.close()

    if as_json:
        console.print_json(json.dumps([s.__dict__ for s in shops], ensure_ascii=False))
        return
    table = Table(title=f"매장 {len(shops)}개")
    for col in ("코드", "구분", "매장명"):
        table.add_column(col, overflow="fold")
    for shop in shops:
        table.add_row(shop.code, shop.group, shop.clean_name)
    console.print(table)


@app.command("init-db")
def init_db() -> None:
    """Create the Postgres schema."""
    cfg = load_config()
    store = _store_or_exit(cfg)
    tables = store.init_schema()
    missing = {"program", "record", "scrape_run"} - set(tables)
    if missing:
        console.print(f"[red]실패[/] 누락된 테이블: {', '.join(sorted(missing))}")
        raise typer.Exit(1)
    console.print(f"[green]OK[/] okpos 스키마 확인됨: {', '.join(tables)}")


@app.command("scrape")
def scrape_cmd(  # noqa: PLR0913
    date_from: str = typer.Option(..., "--from", help="Start date, YYYY-MM-DD"),
    date_to: str = typer.Option("", "--to", help="End date (defaults to --from)"),
    filter_class: str = typer.Option("", "--class", help="Only this menu class"),
    shop_cd: str = typer.Option("", "--shop", help="Single shop code for shop-scoped screens"),
    all_shops: bool = typer.Option(
        False, "--all-shops", help="Repeat shop-scoped screens for every visible shop"
    ),
    full: bool = typer.Option(False, "--full", help="Ignore prior runs and re-scrape"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve screens, do not query"),
    to_db: bool = typer.Option(True, "--to-db/--no-db", help="Persist to Postgres"),
    limit: int = typer.Option(0, "--limit", help="Cap the number of screens (debugging)"),
) -> None:
    """Crawl every program in the catalogue for the given date range."""
    if shop_cd and all_shops:
        raise typer.BadParameter("--shop과 --all-shops는 함께 쓸 수 없습니다")

    start = _parse_day(date_from)
    end = _parse_day(date_to) if date_to else start
    days = date_range(start, end)

    cfg, throttle, session = _connect()
    client = OkposClient(session, throttle, cfg)
    programs = fetch_catalog(session, throttle)
    if filter_class:
        needle = filter_class.lower()
        programs = [
            p for p in programs
            if needle in p.l_class.lower() or needle in p.m_class.lower()
        ]

    store = None
    if to_db and cfg.has_db:
        store = Store(cfg.pg_dsn)
        store.init_schema()
        store.save_programs(programs)
    elif to_db:
        console.print("[yellow]OKPOS_PG_DSN 미설정 — DB 적재를 건너뜁니다[/]")

    shop_codes: list[str] = []
    if all_shops:
        with console.status("[cyan]매장 목록을 가져오는 중..."):
            shops = fetch_shops(client)
        shop_codes = [s.code for s in shops]
        if not shop_codes:
            # fetch_shops may have re-logged in; close whatever is live now.
            client.session.close()
            console.print(
                "[red]--all-shops를 요청했으나 조회된 매장이 0개입니다.[/] "
                "기본 범위로 조용히 넘어가지 않고 중단합니다."
            )
            raise typer.Exit(1)
        console.print(f"매장 [bold]{len(shop_codes)}[/]개를 순회합니다")

    with console.status(f"[cyan]{len(programs)}개 프로그램의 조회 화면을 해석하는 중..."):
        targets = resolve_targets(client, programs, progress=console.print)
    if limit:
        targets = targets[:limit]
    shop_scoped = sum(1 for t in targets if t.spec.needs_shop)
    console.print(
        f"조회 가능한 화면 [bold]{len(targets)}[/]개"
        f" (매장별 {shop_scoped}개) · 대상 날짜 {len(days)}일"
    )
    if shop_codes:
        planned = (len(targets) - shop_scoped) + shop_scoped * len(shop_codes)
        console.print(f"예상 조회 조합: 약 [bold]{planned * len(days):,}[/]건")

    if dry_run:
        table = Table(title="조회 대상 (dry-run)")
        for col in ("컨트롤러", "시트", "날짜필드", "매장필요", "컬럼"):
            table.add_column(col, overflow="fold")
        for t in targets:
            table.add_row(
                t.spec.controller, str(t.spec.sheet_count),
                ",".join(t.spec.date_fields) or "-",
                "Y" if t.spec.needs_shop else "-", str(len(t.spec.columns)),
            )
        console.print(table)
        client.session.close()
        return

    stats = scrape(
        client, targets, days, store=store, incremental=not full,
        shop_cd=shop_cd, shops=shop_codes or None, progress=console.print,
    )
    # `session` may have been replaced by a re-login; close the live one.
    client.session.close()

    console.print()
    console.print(
        f"[bold green]완료[/] 화면 {stats.screens} · 조회 {stats.searches} · "
        f"행 {stats.rows} · 스킵 {stats.skipped} · 실패 {len(stats.failures)}"
    )
    console.print(f"실측 피크 {throttle.observed_peak_rps():.0f} RPS (상한 {cfg.max_rps})")
    adaptive_summary = summarize_adaptive_throttle(throttle)
    if adaptive_summary:
        console.print(adaptive_summary)
    for name, msg, count in summarize_failures(stats.failures):
        times = f" ×{count}" if count > 1 else ""
        console.print(f"  [red]![/] {name}{times}: {msg}")


@app.command("export")
def export_cmd(
    out: str = typer.Option("", "--out", help="Output .xlsx path"),
    controller: str = typer.Option("", "--controller", help="Only this controller"),
    date_from: str = typer.Option("", "--from", help="Start date, YYYY-MM-DD"),
    date_to: str = typer.Option("", "--to", help="End date, YYYY-MM-DD"),
) -> None:
    """Export collected records from Postgres into an xlsx workbook."""
    cfg = load_config()
    store = _store_or_exit(cfg)
    d_from = _parse_day(date_from) if date_from else None
    d_to = _parse_day(date_to) if date_to else None

    records = store.fetch_records(
        controller=controller or None, date_from=d_from, date_to=d_to
    )
    if not records:
        console.print("[yellow]조건에 맞는 레코드가 없습니다[/]")
        raise typer.Exit(1)

    path = Path(out) if out else Path("exports") / default_export_name(d_from, d_to)
    written, sheets, rows = write_xlsx(records, path)
    console.print(f"[green]OK[/] {written} · 시트 {sheets}개 · 행 {rows}개")


@app.command()
def status() -> None:
    """Show what has been collected so far."""
    cfg = load_config()
    store = _store_or_exit(cfg)
    rows = store.controllers()
    if not rows:
        console.print("[yellow]수집된 데이터가 없습니다[/]")
        return
    table = Table(title="수집 현황")
    for col in ("컨트롤러", "프로그램", "최초", "최종", "행"):
        table.add_column(col, overflow="fold")
    total = 0
    for r in rows:
        total += r["rows"]
        table.add_row(
            r["controller"], r["name"], str(r["first_date"]),
            str(r["last_date"]), f"{r['rows']:,}",
        )
    console.print(table)
    console.print(f"합계 [bold]{total:,}[/] 행")



def main() -> None:
    app()


if __name__ == "__main__":
    main()
