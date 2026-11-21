"""Sellable area-hours, shared-slot positions and held/closure deductions.

Rules implemented here
----------------------
* Effective sellable area is gross area minus corridor/security exclusions.
* Hours are half-open integer clock hours.
* A lecture or closure hold removes those hours from *everyone's* opportunity;
  held hours are never treated as space that will simply be free later.
* Confirmed share percentages carve the effective area.  The unconfirmed
  remainder is reported as a bounded range and is never split evenly.
* A studio's own display window, not the whole opening window, bounds its
  sellable hours; the difference is reported as unallocated time.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any, Iterable

from .model import DrillData
from .timeutil import Interval, intersect, intersect_lists, length, subtract


def effective_cm2(layout_row: dict[str, Any]) -> int:
    """Sellable area after corridor/security exclusions."""
    return max(0, int(layout_row["gross_cm2"]) - int(layout_row["exclusion_cm2"]))


def available_intervals(data: DrillData, cabinet: str, day: str, window: Interval) -> list[Interval]:
    """Opening window minus any lecture/closure holds on that cabinet and day."""
    return subtract(window, data.hold_intervals(cabinet, day))


def held_hours(data: DrillData, cabinet: str, day: str, window: Interval) -> int:
    return length(intersect(data.hold_intervals(cabinet, day), window))


def _unit(row: dict[str, Any]) -> dict[str, Any]:
    """The fields common to operation and next_layout layout records."""
    return {key: row[key] for key in ("cabinet", "layout", "date", "from_hour", "to_hour", "gross_cm2", "exclusion_cm2")}


@dataclass
class OperationCapacity:
    operation: str
    cabinet: str
    layout: str
    date: str
    effective_cm2: int
    open_hours: int
    held_hours: int
    available_hours: int
    area_hours: int
    display_covered_hours: int
    display_area_hours: int
    unallocated_hours: int
    unallocated_area_hours: int
    holds: list[dict[str, Any]] = field(default_factory=list)
    available_windows: list[Interval] = field(default_factory=list)
    display_windows: list[Interval] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("available_windows", "display_windows"):
            result[key] = [list(i) for i in result[key]]
        return result


def operation_capacity(data: DrillData, operation: dict[str, Any]) -> OperationCapacity:
    """Physical and actually-sellable opportunity for one operation row.

    Held hours, product-display hours and unallocated hours are partitioned from
    the same opening window so they never overlap and never double-count:

        open = held + product-display + unallocated

    ``display_covered_hours`` counts only non-held display hours.  Hours inside a
    hold are never counted as display opportunity even if a product row nominally
    spans them.
    """
    window = data.operation_window(operation)
    effective = effective_cm2(operation)
    holds = data.holds_for(operation["cabinet"], operation["date"])
    available = available_intervals(data, operation["cabinet"], operation["date"], window)
    available_hours = length(available)
    open_hours = window[1] - window[0]
    display = data.display_windows(operation["id"])
    covered = intersect_lists(display, available)
    covered_hours = length(covered)
    unallocated = subtract(window, data.hold_intervals(operation["cabinet"], operation["date"]) + covered)
    return OperationCapacity(
        operation=operation["id"],
        cabinet=operation["cabinet"],
        layout=operation["layout"],
        date=operation["date"],
        effective_cm2=effective,
        open_hours=open_hours,
        held_hours=held_hours(data, operation["cabinet"], operation["date"], window),
        available_hours=available_hours,
        area_hours=effective * available_hours,
        display_covered_hours=covered_hours,
        display_area_hours=effective * covered_hours,
        unallocated_hours=length(unallocated),
        unallocated_area_hours=effective * length(unallocated),
        holds=list(holds),
        available_windows=available,
        display_windows=covered,
    )


@dataclass
class StudioPosition:
    operation: str
    cabinet: str
    layout: str
    date: str
    studio: str
    confirmed_percent: Fraction | None
    area_cm2_low: Fraction
    area_cm2_high: Fraction
    area_cm2_point: Fraction | None
    hours: int
    area_hours_low: Fraction
    area_hours_high: Fraction
    area_hours_point: Fraction | None
    residual_percent: Fraction
    confirmed: bool
    display_hours: int = 0
    undisplaced_hours: int = 0

    def to_dict(self) -> dict[str, Any]:
        def number(value: Fraction | None) -> float | None:
            return None if value is None else float(value)

        return {
            "operation": self.operation,
            "cabinet": self.cabinet,
            "layout": self.layout,
            "date": self.date,
            "studio": self.studio,
            "confirmed_percent": number(self.confirmed_percent),
            "area_cm2_low": number(self.area_cm2_low),
            "area_cm2_high": number(self.area_cm2_high),
            "area_cm2_point": number(self.area_cm2_point),
            "hours": self.hours,
            "display_hours": self.display_hours,
            "undisplayed_hours": self.undisplaced_hours,
            "area_hours_low": number(self.area_hours_low),
            "area_hours_high": number(self.area_hours_high),
            "area_hours_point": number(self.area_hours_point),
            "residual_percent": number(self.residual_percent),
            "confirmed": self.confirmed,
        }


def share_positions(data: DrillData, operation: dict[str, Any]) -> list[StudioPosition]:
    """Positions for every studio sharing a cabinet on one operation row.

    The position window is the studio's signed share interval clipped to the
    operation window and to non-held hours.  Product display windows are tracked
    separately: a display window shorter than the share window leaves signed
    hours without goods on show, which is reported rather than assumed away.

    The confirmed percentages are used as signed.  The remainder belongs to the
    unconfirmed studios only as an *upper bound*; it is not divided among them.
    """
    capacity = operation_capacity(data, operation)
    shares = data.shares_by_operation.get(operation["id"], [])
    confirmed_sum = sum(
        (Fraction(row["confirmed_percent"], 100) for row in shares if row["confirmed_percent"] is not None),
        Fraction(0),
    )
    residual = max(Fraction(0), Fraction(1) - confirmed_sum)
    positions: list[StudioPosition] = []
    for share in shares:
        studio = share["studio"]
        signed = intersect([(share["from_hour"], share["to_hour"])], data.operation_window(operation))
        share_hours = intersect_lists(capacity.available_windows, signed)
        hours = length(share_hours)
        display_hours = length(intersect_lists(capacity.available_windows, data.display_windows(operation["id"], studio)))
        percent = None if share["confirmed_percent"] is None else Fraction(share["confirmed_percent"], 100)
        if percent is not None:
            low = high = capacity.effective_cm2 * percent * hours
            point = low
            low_cm2 = high_cm2 = Fraction(capacity.effective_cm2) * percent
            point_cm2 = low_cm2
        else:
            low = Fraction(0)
            high = capacity.effective_cm2 * residual * hours
            point = None
            low_cm2 = Fraction(0)
            high_cm2 = Fraction(capacity.effective_cm2) * residual
            point_cm2 = None
        positions.append(
            StudioPosition(
                operation=operation["id"],
                cabinet=operation["cabinet"],
                layout=operation["layout"],
                date=operation["date"],
                studio=studio,
                confirmed_percent=percent,
                area_cm2_low=low_cm2,
                area_cm2_high=high_cm2,
                area_cm2_point=point_cm2,
                hours=hours,
                area_hours_low=low,
                area_hours_high=high,
                area_hours_point=point,
                residual_percent=residual,
                confirmed=percent is not None,
                display_hours=display_hours,
                undisplaced_hours=max(0, hours - display_hours),
            )
        )
    return positions


def aggregate_positions(positions: Iterable[StudioPosition]) -> dict[tuple[str, str], dict[str, Any]]:
    """Sum positions by (studio, cabinet), keeping point/range separation."""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for position in positions:
        key = (position.studio, position.cabinet)
        entry = totals.setdefault(
            key,
            {
                "studio": position.studio,
                "cabinet": position.cabinet,
                "hours": 0,
                "area_hours_low": Fraction(0),
                "area_hours_high": Fraction(0),
                "area_hours_point": Fraction(0),
                "point_complete": True,
                "has_unconfirmed": False,
                "operations": [],
                "layouts": set(),
            },
        )
        entry["hours"] += position.hours
        entry["area_hours_low"] += position.area_hours_low
        entry["area_hours_high"] += position.area_hours_high
        if position.area_hours_point is None:
            entry["point_complete"] = False
        else:
            entry["area_hours_point"] += position.area_hours_point
        entry["has_unconfirmed"] |= not position.confirmed
        entry["operations"].append(position.operation)
        entry["layouts"].add(position.layout)
    for entry in totals.values():
        entry["layouts"] = sorted(entry["layouts"])
    return totals


def cabinet_capacity_summary(data: DrillData, cabinet: str | None = None, layout: str | None = None) -> list[dict[str, Any]]:
    """Aggregate physical and sellable area-hours per cabinet/layout."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for operation in data.raw["operation"]:
        if cabinet is not None and operation["cabinet"] != cabinet:
            continue
        if layout is not None and operation["layout"] != layout:
            continue
        capacity = operation_capacity(data, operation)
        key = (capacity.cabinet, capacity.layout)
        bucket = buckets.setdefault(
            key,
            {
                "cabinet": capacity.cabinet,
                "layout": capacity.layout,
                "operations": [],
                "effective_cm2": capacity.effective_cm2,
                "open_hours": 0,
                "held_hours": 0,
                "available_hours": 0,
                "area_hours": 0,
                "display_covered_hours": 0,
                "display_area_hours": 0,
                "unallocated_hours": 0,
                "unallocated_area_hours": 0,
                "holds": [],
            },
        )
        for field_name in ("open_hours", "held_hours", "available_hours", "area_hours", "display_covered_hours", "display_area_hours", "unallocated_hours", "unallocated_area_hours"):
            bucket[field_name] += getattr(capacity, field_name)
        bucket["operations"].append(capacity.operation)
        bucket["holds"].extend(capacity.holds)
    return [buckets[key] for key in sorted(buckets)]


def studio_capacity_summary(data: DrillData, studio: str | None = None) -> list[dict[str, Any]]:
    """Aggregate studio positions by (studio, cabinet), with explicit ranges.

    This is a *contract total* across all observed layout versions, not a
    like-for-like comparison: an area-hour under V1 is not the same physical slot
    as one under V2.  Per-layout figures are in :func:`position_performance` and
    :func:`~cabinet_analytics.revenue.studio_yield_table`.
    """
    positions: list[StudioPosition] = []
    for operation in data.raw["operation"]:
        positions.extend(share_positions(data, operation))
    totals = aggregate_positions(positions)
    results = []
    for key in sorted(totals):
        entry = totals[key]
        if studio is not None and entry["studio"] != studio:
            continue
        point = entry["area_hours_point"] if entry["point_complete"] else None
        results.append(
            {
                "studio": entry["studio"],
                "cabinet": entry["cabinet"],
                "layouts": entry["layouts"],
                "hours": entry["hours"],
                "area_hours_point": None if point is None else float(point),
                "area_hours_low": float(entry["area_hours_low"]),
                "area_hours_high": float(entry["area_hours_high"]),
                "has_unconfirmed": entry["has_unconfirmed"],
                "operations": entry["operations"],
                "note": "contract total across layout versions; use per-layout yields for comparisons",
            }
        )
    return results


def next_capacity(data: DrillData, cabinet: str, day: str) -> dict[str, Any]:
    """Sellable area-hours for a planned layout, after any held/closure hours."""
    rows = data.next_layout_by_cabinet_date.get((cabinet, day))
    if not rows:
        return {}
    row = rows[0]
    window = (row["from_hour"], row["to_hour"])
    effective = effective_cm2(row)
    available = available_intervals(data, cabinet, day, window)
    hours = length(available)
    return {
        "cabinet": cabinet,
        "layout": row["layout"],
        "date": day,
        "effective_cm2": effective,
        "open_hours": window[1] - window[0],
        "held_hours": held_hours(data, cabinet, day, window),
        "available_hours": hours,
        "area_hours": effective * hours,
        "available_windows": [list(i) for i in available],
        "holds": data.holds_for(cabinet, day),
    }
