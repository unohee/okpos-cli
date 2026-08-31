"""Crawl orchestration.

Walks the runtime catalogue, expands tabbed screens into their real children,
and searches every sheet of every screen across the requested date range.
Nothing about the screen list is hard-coded: it all comes from `menuv.jsp`
and from each screen's own HTML.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .catalog import Program
from .client import OkposClient
from .db import Store
from .screen import ScreenSpec

Progress = Callable[[str], None]


def date_range(start: date, end: date) -> list[date]:
    if start > end:
        start, end = end, start
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


@dataclass
class Target:
    """One resolved screen ready to be searched."""

    program: Program
    spec: ScreenSpec


@dataclass
class ScrapeStats:
    screens: int = 0
    searches: int = 0
    rows: int = 0
    skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "screens": self.screens,
            "searches": self.searches,
            "rows": self.rows,
            "skipped": self.skipped,
            "failures": len(self.failures),
        }


def resolve_targets(
    client: OkposClient,
    programs: Iterable[Program],
    progress: Progress | None = None,
) -> list[Target]:
    """Turn catalogue entries into queryable screens, expanding tabbed shells."""
    targets: list[Target] = []
    seen: set[str] = set()
    for prog in programs:
        try:
            spec = client.get_screen(prog.screen_path, prog.query_params)
        except Exception as exc:  # noqa: BLE001 - one bad screen must not stop the crawl
            if progress:
                progress(f"[yellow]skip[/] {prog.screen_path}: {type(exc).__name__}")
            continue

        if spec.is_tabbed:
            for child in spec.tab_children:
                if child in seen:
                    continue
                try:
                    child_spec = client.get_screen(child)
                except Exception:  # noqa: BLE001
                    continue
                if child_spec.queryable:
                    seen.add(child)
                    targets.append(Target(prog, child_spec))
            continue

        if spec.queryable and spec.path not in seen:
            seen.add(spec.path)
            targets.append(Target(prog, spec))
    return targets


def _persist_failure(
    store: Store | None, spec: ScreenSpec, seq: int, scope: str, biz_date: date, message: str
) -> None:
    if store:
        store.mark_run(
            controller=spec.controller, sheet_seq=seq, shop_cd=scope,
            biz_date=biz_date, screen_path=spec.path, row_count=0,
            status="error", message=message,
        )


def _scopes_for(spec: ScreenSpec, shops: list[str] | None, shop_cd: str) -> list[str]:
    """Which shop codes to repeat this screen for.

    Only shop-scoped screens are worth repeating; the rest would return the
    same rows once per shop.
    """
    if not spec.needs_shop:
        return [""]
    return shops if shops else [shop_cd]


def scrape(  # noqa: PLR0913
    client: OkposClient,
    targets: list[Target],
    dates: list[date],
    *,
    store: Store | None = None,
    incremental: bool = True,
    shop_cd: str = "",
    shops: list[str] | None = None,
    progress: Progress | None = None,
    sink: Callable[[Target, int, date, list[dict[str, Any]]], None] | None = None,
) -> ScrapeStats:
    """Search every target across every shop scope and date, persisting results."""
    stats = ScrapeStats(screens=len(targets))
    use_state = bool(store and incremental)
    done = store.completed_keys(dates) if use_state else set()
    # Shop-agnostic screens are matched without shop_cd; see completed_any_scope.
    done_any = store.completed_any_scope(dates) if use_state else set()

    for target in targets:
        spec = target.spec
        for scope in _scopes_for(spec, shops, shop_cd):
            for biz_date in dates:
                overrides = {f: biz_date.isoformat() for f in spec.date_fields}
                if scope:
                    overrides["ss_SHOP_CD"] = scope
                _search_sheets(
                    client, target, scope, biz_date, overrides,
                    store=store, done=done, done_any=done_any,
                    stats=stats, progress=progress, sink=sink,
                )
    return stats


def _already_done(  # noqa: PLR0913
    spec: ScreenSpec,
    seq: int,
    scope: str,
    biz_date: date,
    done: set[tuple[str, int, str, date]],
    done_any: set[tuple[str, int, date]],
) -> bool:
    """Whether this sheet was already collected.

    Shop-scoped screens are keyed by shop; shop-agnostic ones are not, so a run
    recorded under any scope (including an older `--shop` run) counts as done.
    """
    if spec.needs_shop:
        return (spec.controller, seq, scope, biz_date) in done
    return (spec.controller, seq, biz_date) in done_any


def _search_sheets(  # noqa: PLR0913
    client: OkposClient,
    target: Target,
    scope: str,
    biz_date: date,
    overrides: dict[str, str],
    *,
    store: Store | None,
    done: set[tuple[str, int, str, date]],
    done_any: set[tuple[str, int, date]],
    stats: ScrapeStats,
    progress: Progress | None,
    sink: Callable[[Target, int, date, list[dict[str, Any]]], None] | None,
) -> None:
    """Run every sheet of one screen for one (shop, date) combination."""
    spec = target.spec
    iso = biz_date.isoformat()
    for seq in range(1, spec.sheet_count + 1):
        if _already_done(spec, seq, scope, biz_date, done, done_any):
            stats.skipped += 1
            continue
        try:
            result = client.search(spec, seq, overrides)
        except Exception as exc:  # noqa: BLE001 - one bad sheet must not stop the crawl
            stats.failures.append((f"{spec.controller}#{seq}@{iso}", str(exc)))
            _persist_failure(store, spec, seq, scope, biz_date, str(exc))
            continue

        stats.searches += 1
        if not result.ok:
            stats.failures.append(
                (f"{spec.controller}#{seq}@{iso}", f"code={result.code} {result.message}")
            )
            _persist_failure(store, spec, seq, scope, biz_date, result.message)
            continue

        stats.rows += len(result.rows)
        if store:
            store.save_rows(
                program_cd=target.program.code, controller=spec.controller,
                screen_path=spec.path, sheet_seq=seq, shop_cd=scope,
                biz_date=biz_date, rows=result.rows,
            )
            store.mark_run(
                controller=spec.controller, sheet_seq=seq, shop_cd=scope,
                biz_date=biz_date, screen_path=spec.path,
                row_count=len(result.rows), status="ok",
            )
        if sink:
            sink(target, seq, biz_date, result.rows)
        if progress and result.rows:
            label = f" [{scope}]" if scope else ""
            progress(
                f"[green]{len(result.rows):>5}[/] rows  "
                f"{target.program.name} #{seq}{label} @ {iso}"
            )
