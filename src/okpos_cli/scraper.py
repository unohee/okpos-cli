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


def scrape(
    client: OkposClient,
    targets: list[Target],
    dates: list[date],
    *,
    store: Store | None = None,
    incremental: bool = True,
    shop_cd: str = "",
    progress: Progress | None = None,
    sink: Callable[[Target, int, date, list[dict[str, Any]]], None] | None = None,
) -> ScrapeStats:
    """Search every target for every date, persisting through `store`/`sink`."""
    stats = ScrapeStats(screens=len(targets))
    done = store.completed_keys(dates) if (store and incremental) else set()

    for target in targets:
        spec = target.spec
        for biz_date in dates:
            iso = biz_date.isoformat()
            overrides = {f: iso for f in spec.date_fields}
            if shop_cd and spec.needs_shop:
                overrides["ss_SHOP_CD"] = shop_cd

            for seq in range(1, spec.sheet_count + 1):
                key = (spec.controller, seq, shop_cd, biz_date)
                if key in done:
                    stats.skipped += 1
                    continue
                try:
                    result = client.search(spec, seq, overrides)
                except Exception as exc:  # noqa: BLE001 - one bad sheet must not stop the crawl
                    stats.failures.append((f"{spec.controller}#{seq}@{iso}", str(exc)))
                    if store:
                        store.mark_run(
                            controller=spec.controller, sheet_seq=seq, shop_cd=shop_cd,
                            biz_date=biz_date, screen_path=spec.path, row_count=0,
                            status="error", message=str(exc),
                        )
                    continue

                stats.searches += 1
                if not result.ok:
                    stats.failures.append(
                        (f"{spec.controller}#{seq}@{iso}", f"code={result.code} {result.message}")
                    )
                    if store:
                        store.mark_run(
                            controller=spec.controller, sheet_seq=seq, shop_cd=shop_cd,
                            biz_date=biz_date, screen_path=spec.path, row_count=0,
                            status="error", message=result.message,
                        )
                    continue

                stats.rows += len(result.rows)
                if store:
                    store.save_rows(
                        program_cd=target.program.code, controller=spec.controller,
                        screen_path=spec.path, sheet_seq=seq, shop_cd=shop_cd,
                        biz_date=biz_date, rows=result.rows,
                    )
                    store.mark_run(
                        controller=spec.controller, sheet_seq=seq, shop_cd=shop_cd,
                        biz_date=biz_date, screen_path=spec.path,
                        row_count=len(result.rows), status="ok",
                    )
                if sink:
                    sink(target, seq, biz_date, result.rows)
                if progress and result.rows:
                    progress(
                        f"[green]{len(result.rows):>5}[/] rows  "
                        f"{target.program.name} #{seq} @ {iso}"
                    )
    return stats
