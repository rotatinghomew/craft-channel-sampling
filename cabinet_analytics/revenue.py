"""Revenue attribution, reconciliation and per-position performance.

Attribution rules
-----------------
* Every checkout carrying a ``take`` is attributed to the *take's* cabinet and
  layout.  A delayed checkout or a checkout rung up at another cash desk does
  not move the revenue to the payment date or the cash desk.
* Checkouts with no ``take`` are reported as unattributed revenue.  They are
  never quietly spread across cabinets, studios or plans; the report proves that
  attributed + unattributed equals the cashier total.
* Takes without a checkout are reported as *identified but unsettled* pickups.
  They are not treated as zero revenue and are not merged into unattributed
  revenue without evidence.
* Visitors are observed per product.  They are never summed across studios or
  products as independent footfall; the only ratio shown is pickups per observed
  visitor on the same product row.
* Revenue per sellable area-hour uses the studio's own signed share as the
  denominator.  For an unconfirmed share only a range is produced (zero to the
  whole residual); the residual is never split evenly between studios.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .capacity import operation_capacity, share_positions
from .model import DrillData
from .timeutil import intersect_lists, length

STATUS_PENDING = "identified_unsettled"
NO_DISPLAY_SKU = "__no_display__"


@dataclass
class RevenueLedger:
    cashier_total_cents: int
    attributed_cents: int
    unattributed_cents: int
    attributed_takes: int
    pending_takes: int
    pending_take_rows: list[dict[str, Any]]
    unattributed_rows: list[dict[str, Any]]
    cross_desk_rows: list[dict[str, Any]]
    delayed_rows: list[dict[str, Any]]
    by_cabinet: dict[tuple[str, str], dict[str, Any]]
    by_studio: dict[str, dict[str, Any]]
    by_take: dict[str, dict[str, Any]]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def reconciles(self) -> bool:
        return self.attributed_cents + self.unattributed_cents == self.cashier_total_cents

    def to_dict(self) -> dict[str, Any]:
        def serialise(entry: dict[str, Any]) -> dict[str, Any]:
            result = dict(entry)
            if isinstance(result.get("take_ids"), set):
                result["take_ids"] = sorted(result["take_ids"])
            return result

        return {
            "cashier_total_cents": self.cashier_total_cents,
            "attributed_cents": self.attributed_cents,
            "unattributed_cents": self.unattributed_cents,
            "reconciles": self.reconciles,
            "attributed_takes": self.attributed_takes,
            "pending_takes": self.pending_takes,
            "pending_take_rows": self.pending_take_rows,
            "unattributed_rows": self.unattributed_rows,
            "cross_desk_rows": self.cross_desk_rows,
            "delayed_rows": self.delayed_rows,
            "by_cabinet": {f"{k[0]}:{k[1]}": serialise(v) for k, v in sorted(self.by_cabinet.items())},
            "by_studio": {k: serialise(v) for k, v in sorted(self.by_studio.items())},
            "by_take": self.by_take,
            "diagnostics": self.diagnostics,
            "issues": self.issues,
        }


def _cabinet_entry(cabinet: str, layout: str) -> dict[str, Any]:
    return {"cabinet": cabinet, "layout": layout, "revenue_cents": 0, "takes": 0, "pending_takes": 0, "take_ids": set()}


def _studio_entry(studio: str) -> dict[str, Any]:
    return {"studio": studio, "revenue_cents": 0, "takes": 0, "pending_takes": 0, "take_ids": set(), "skus": set()}


def _linked_take_ids(data: DrillData) -> set[str]:
    return {checkout["take"] for checkout in data.raw["checkout"] if checkout["take"]}


def _sku_net_bounds(data: DrillData) -> dict[str, tuple[int, int, Fraction]]:
    """Observed net range and mean per sku among linked checkouts."""
    values: dict[str, list[int]] = defaultdict(list)
    for checkout in data.raw["checkout"]:
        if checkout["take"] is not None and checkout["take"] in data.takes:
            values[checkout["sku"]].append(checkout["net"])
    return {sku: (min(a), max(a), Fraction(sum(a), len(a))) for sku, a in values.items()}


def pending_value_range(data: DrillData, studio: str | None = None) -> dict[str, Any]:
    """Value band for identified-but-unsettled pickups, by observed sku price."""
    bounds = _sku_net_bounds(data)
    linked = _linked_take_ids(data)
    typed: Counter[str] = Counter()
    for take in data.raw["take"]:
        if take["id"] in linked:
            continue
        if studio is not None and data.studio_for(take["operation"], take["sku"]) != studio:
            continue
        typed[take["sku"]] += 1
    low = sum(typed[sku] * bounds[sku][0] for sku in typed if sku in bounds)
    high = sum(typed[sku] * bounds[sku][1] for sku in typed if sku in bounds)
    mean = sum((typed[sku] * bounds[sku][2] for sku in typed if sku in bounds), Fraction(0))
    return {
        "scope": "all" if studio is None else studio,
        "takes": sum(typed.values()),
        "by_sku": dict(sorted(typed.items())),
        "low_cents": low,
        "high_cents": high,
        "mean_cents": float(mean),
        "basis": "observed net range per sku among linked checkouts; a valuation band, not an attribution",
    }


def build_ledger(data: DrillData) -> RevenueLedger:
    """Attribute revenue and reconcile it to the cashier total."""
    by_cabinet: dict[tuple[str, str], dict[str, Any]] = {}
    by_studio: dict[str, dict[str, Any]] = {}
    by_take: dict[str, dict[str, Any]] = {}
    pending_rows: list[dict[str, Any]] = []
    unattributed_rows: list[dict[str, Any]] = []
    cross_desk: list[dict[str, Any]] = []
    delayed: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    attributed = 0
    cashier = 0
    linked = _linked_take_ids(data)

    for checkout in data.raw["checkout"]:
        cashier += checkout["net"]
        take_id = checkout["take"]
        if take_id is None:
            unattributed_rows.append(
                {"checkout": checkout["id"], "sku": checkout["sku"], "cash_desk": checkout["cash_desk"], "date": checkout["date"], "net": checkout["net"], "status": "unattributed"}
            )
            continue
        take = data.takes.get(take_id)
        operation = data.operations.get(take["operation"]) if take else None
        if take is None or operation is None:
            unattributed_rows.append(
                {
                    "checkout": checkout["id"],
                    "take": take_id,
                    "sku": checkout["sku"],
                    "cash_desk": checkout["cash_desk"],
                    "date": checkout["date"],
                    "net": checkout["net"],
                    "status": "unattributed",
                    "reason": "unknown take or operation; needs manual evidence",
                }
            )
            continue
        studio = data.studio_for(take["operation"], take["sku"])
        attributed += checkout["net"]
        key = (operation["cabinet"], operation["layout"])
        cabinet_entry = by_cabinet.setdefault(key, _cabinet_entry(*key))
        cabinet_entry["revenue_cents"] += checkout["net"]
        cabinet_entry["takes"] += 1
        cabinet_entry["take_ids"].add(take_id)
        studio_entry = by_studio.setdefault(studio, _studio_entry(studio))
        studio_entry["revenue_cents"] += checkout["net"]
        studio_entry["takes"] += 1
        studio_entry["take_ids"].add(take_id)
        studio_entry["skus"].add(take["sku"])
        by_take[take_id] = {
            "take": take_id,
            "operation": take["operation"],
            "cabinet": operation["cabinet"],
            "layout": operation["layout"],
            "studio": studio,
            "sku": take["sku"],
            "take_date": take["date"],
            "take_hour": take["hour"],
            "checkout": checkout["id"],
            "checkout_date": checkout["date"],
            "cash_desk": checkout["cash_desk"],
            "net": checkout["net"],
        }
        if checkout["cash_desk"] != operation["cabinet"]:
            cross_desk.append({"take": take_id, "original_cabinet": operation["cabinet"], "cash_desk": checkout["cash_desk"], "net": checkout["net"], "date": checkout["date"]})
        if checkout["date"] != take["date"]:
            delayed.append({"take": take_id, "take_date": take["date"], "checkout_date": checkout["date"], "net": checkout["net"]})
        if any(row["from_hour"] <= take["hour"] < row["to_hour"] for row in data.holds_for(operation["cabinet"], operation["date"])):
            issues.append(
                {
                    "code": "revenue_during_hold",
                    "severity": "warning",
                    "detail": "pickup attributed to a cabinet hour held for a lecture or closure; revenue is kept, the hour is not counted as display opportunity",
                    "ids": [take_id, checkout["id"]],
                }
            )

    for take in data.raw["take"]:
        if take["id"] in linked:
            continue
        operation = data.operations.get(take["operation"])
        studio = data.studio_for(take["operation"], take["sku"]) if operation else None
        pending_rows.append(
            {
                "take": take["id"],
                "operation": take["operation"],
                "cabinet": operation["cabinet"] if operation else None,
                "layout": operation["layout"] if operation else None,
                "studio": studio,
                "sku": take["sku"],
                "date": take["date"],
                "hour": take["hour"],
                "status": STATUS_PENDING,
            }
        )
        if operation:
            by_cabinet.setdefault((operation["cabinet"], operation["layout"]), _cabinet_entry(operation["cabinet"], operation["layout"]))["pending_takes"] += 1
            studio_entry = by_studio.setdefault(studio, _studio_entry(studio))
            studio_entry["pending_takes"] += 1
            studio_entry["skus"].add(take["sku"])

    # Diagnostic only: unattributed checkouts and pending pickups can share a
    # sku and day without being the same physical item.  Counts are shown so an
    # operator can see why no automatic pairing was attempted.
    unattributed_by = Counter((row["sku"], row["date"]) for row in unattributed_rows)
    pending_by = Counter((row["sku"], row["date"]) for row in pending_rows)
    diagnostics = {
        "unattributed_by_sku_date": {f"{sku}:{day}": count for (sku, day), count in sorted(unattributed_by.items())},
        "pending_by_sku_date": {f"{sku}:{day}": count for (sku, day), count in sorted(pending_by.items())},
        "note": "same sku/day counts are not enough to pair a checkout with a pickup; pairing requires a take id on the checkout",
    }
    for entry in by_studio.values():
        entry["skus"] = sorted(entry["skus"])

    return RevenueLedger(
        cashier_total_cents=cashier,
        attributed_cents=attributed,
        unattributed_cents=cashier - attributed,
        attributed_takes=sum(entry["takes"] for entry in by_cabinet.values()),
        pending_takes=len(pending_rows),
        pending_take_rows=pending_rows,
        unattributed_rows=unattributed_rows,
        cross_desk_rows=cross_desk,
        delayed_rows=delayed,
        by_cabinet=by_cabinet,
        by_studio=by_studio,
        by_take=by_take,
        diagnostics=diagnostics,
        issues=issues,
    )


# --------------------------------------------------------------------------- yields


@dataclass
class PositionPerformance:
    studio: str
    cabinet: str
    sku: str
    layout: str
    visitors: int
    takes: int
    linked_takes: int
    pending_takes: int
    revenue_cents: int
    signed_area_hours: Fraction
    displayed_area_hours: Fraction
    confirmed_area_hours: Fraction
    residual_area_hours: Fraction
    display_hours: int
    sellable_display_hours: int
    held_hours: int
    exposure: str
    yield_low: Fraction | None
    yield_high: Fraction | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "studio": self.studio,
            "cabinet": self.cabinet,
            "sku": self.sku,
            "layout": self.layout,
            "visitors": self.visitors,
            "takes": self.takes,
            "linked_takes": self.linked_takes,
            "pending_takes": self.pending_takes,
            "revenue_cents": self.revenue_cents,
            "signed_area_hours": float(self.signed_area_hours),
            "displayed_area_hours": float(self.displayed_area_hours),
            "confirmed_area_hours": float(self.confirmed_area_hours),
            "residual_area_hours": float(self.residual_area_hours),
            "display_hours": self.display_hours,
            "sellable_display_hours": self.sellable_display_hours,
            "held_hours": self.held_hours,
            "exposure": self.exposure,
            "revenue_per_signed_area_hour_low": None if self.yield_low is None else float(self.yield_low),
            "revenue_per_signed_area_hour_high": None if self.yield_high is None else float(self.yield_high),
            "pickups_per_observed_visitor": None if self.visitors == 0 else self.takes / self.visitors,
        }


def _position_entry(studio: str, cabinet: str, sku: str, layout: str) -> dict[str, Any]:
    return {
        "studio": studio,
        "cabinet": cabinet,
        "sku": sku,
        "layout": layout,
        "visitors": 0,
        "takes": 0,
        "linked_takes": 0,
        "pending_takes": 0,
        "revenue": 0,
        "confirmed_ah": Fraction(0),
        "residual_ah": Fraction(0),
        "signed_ah": Fraction(0),
        "displayed_ah": Fraction(0),
        "display_hours": 0,
        "sellable_display_hours": 0,
        "held_hours": 0,
    }


def position_performance(data: DrillData) -> list[PositionPerformance]:
    """Per studio / cabinet / sku position with an explicit opportunity range.

    A studio's *signed slot* area-hours are computed once per operation from its
    signed share, then apportioned across its own product rows by the hours each
    product is actually on show.  A studio that displays two products does not
    get two full slots.  If a studio shows nothing (no display row), all of its
    signed slot stays unassigned and is reported at studio level instead of being
    invented for a product.

    ``yield_low`` divides linked revenue by the widest honest denominator
    (confirmed share plus the whole unconfirmed residual), so an unconfirmed
    studio cannot appear efficient merely because its denominator is small.
    ``yield_high`` adds the pending-pickup valuation band and divides by the
    confirmed share only; it is an upper bound and is absent when no share is
    confirmed.
    """
    rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    for operation in data.raw["operation"]:
        capacity = operation_capacity(data, operation)
        positions = {position.studio: position for position in share_positions(data, operation)}
        displays = data.display_by_operation.get(operation["id"], [])
        studio_displays: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for display in displays:
            studio_displays[display["studio"]].append(display)
        for studio, studio_rows in studio_displays.items():
            position = positions.get(studio)
            weights: list[tuple[dict[str, Any], Fraction]] = []
            sellable_total = Fraction(0)
            raw_total = Fraction(0)
            for display in studio_rows:
                raw_hours = display["to_hour"] - display["from_hour"]
                sellable_hours = length(intersect_lists([(display["from_hour"], display["to_hour"])], capacity.available_windows))
                weights.append((display, Fraction(sellable_hours)))
                sellable_total += sellable_hours
                raw_total += raw_hours
            share_basis = sellable_total if sellable_total else raw_total
            for display, weight in weights:
                key = (display["studio"], operation["cabinet"], display["sku"], operation["layout"])
                entry = rows.setdefault(key, _position_entry(*key))
                entry["visitors"] += display["visitors"]
                raw_hours = display["to_hour"] - display["from_hour"]
                sellable_hours = length(intersect_lists([(display["from_hour"], display["to_hour"])], capacity.available_windows))
                entry["display_hours"] += raw_hours
                entry["sellable_display_hours"] += sellable_hours
                entry["held_hours"] += raw_hours - sellable_hours
                if position is None:
                    # No share record: product exposure is shown, no share invented.
                    entry["signed_ah"] += capacity.effective_cm2 * sellable_hours
                    entry["displayed_ah"] += capacity.effective_cm2 * sellable_hours
                    continue
                fraction = weight / share_basis if share_basis else Fraction(0)
                entry["signed_ah"] += position.area_hours_high * fraction if not position.confirmed else position.area_hours_point * fraction
                if position.confirmed:
                    entry["confirmed_ah"] += position.area_hours_point * fraction
                    share_fraction = position.confirmed_percent
                else:
                    entry["residual_ah"] += position.area_hours_high * fraction
                    share_fraction = position.residual_percent
                entry["displayed_ah"] += capacity.effective_cm2 * share_fraction * sellable_hours

        # Share records for studios with no product row still need an exposure
        # marker, otherwise their signed hours vanish silently.
        for studio, position in positions.items():
            if studio in studio_displays:
                continue
            key = (studio, operation["cabinet"], NO_DISPLAY_SKU, operation["layout"])
            entry = rows.setdefault(key, _position_entry(*key))
            entry["signed_ah"] += position.area_hours_high if not position.confirmed else position.area_hours_point
            if position.confirmed:
                entry["confirmed_ah"] += position.area_hours_point
            else:
                entry["residual_ah"] += position.area_hours_high

    linked = _linked_take_ids(data)
    ledger = build_ledger(data)
    for take in data.raw["take"]:
        operation = data.operations.get(take["operation"])
        if operation is None:
            continue
        studio = data.studio_for(take["operation"], take["sku"])
        if studio is None:
            continue
        key = (studio, operation["cabinet"], take["sku"], operation["layout"])
        entry = rows.setdefault(key, _position_entry(*key))
        entry["takes"] += 1
        if take["id"] in linked:
            entry["linked_takes"] += 1
            entry["revenue"] += ledger.by_take[take["id"]]["net"]
        else:
            entry["pending_takes"] += 1

    pending_mean = _pending_mean_scoped(data)
    results: list[PositionPerformance] = []
    for key in sorted(rows):
        entry = rows[key]
        wide = entry["confirmed_ah"] + entry["residual_ah"]
        pending_value = pending_mean.get((entry["studio"], entry["cabinet"], entry["layout"], entry["sku"]), Fraction(0))
        if wide == 0:
            exposure = "no_sellable_hours"
        elif entry["display_hours"] and entry["sellable_display_hours"] == 0 and entry["takes"] == 0:
            exposure = "held_only"
        elif entry["visitors"] == 0:
            exposure = "no_observed_customers"
        else:
            exposure = "observable"
        low = None if wide == 0 else Fraction(entry["revenue"]) / wide
        high = None
        if entry["confirmed_ah"] > 0:
            high = Fraction(entry["revenue"] + pending_value) / entry["confirmed_ah"]
        elif wide > 0:
            high = Fraction(entry["revenue"] + pending_value) / wide
        results.append(
            PositionPerformance(
                studio=entry["studio"],
                cabinet=entry["cabinet"],
                sku=entry["sku"],
                layout=entry["layout"],
                visitors=entry["visitors"],
                takes=entry["takes"],
                linked_takes=entry["linked_takes"],
                pending_takes=entry["pending_takes"],
                revenue_cents=entry["revenue"],
                signed_area_hours=entry["signed_ah"],
                displayed_area_hours=entry["displayed_ah"],
                confirmed_area_hours=entry["confirmed_ah"],
                residual_area_hours=entry["residual_ah"],
                display_hours=entry["display_hours"],
                sellable_display_hours=entry["sellable_display_hours"],
                held_hours=entry["held_hours"],
                exposure=exposure,
                yield_low=low,
                yield_high=high,
            )
        )
    return results


def _pending_mean_scoped(data: DrillData) -> dict[tuple[str, str, str, str], Fraction]:
    """Mean-value band for pending pickups, per (studio, cabinet, layout, sku)."""
    bounds = _sku_net_bounds(data)
    linked = _linked_take_ids(data)
    counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
    keys: dict[tuple[str, str, str, str], str] = {}
    for take in data.raw["take"]:
        if take["id"] in linked:
            continue
        operation = data.operations.get(take["operation"])
        if operation is None:
            continue
        studio = data.studio_for(take["operation"], take["sku"])
        if studio is None:
            continue
        key = (studio, operation["cabinet"], operation["layout"], take["sku"])
        counts[key] += 1
        keys[key] = take["sku"]
    return {key: count * bounds[keys[key]][2] for key, count in counts.items() if keys[key] in bounds}


def _unattributed_split(data: DrillData, mode: str) -> dict[tuple[str, str, str], Fraction]:
    """Sensitivity-only allocation of checkouts without a take.

    ``visitor_proportional`` and ``equal_per_position`` spread the previous
    day's same-sku checkouts across candidate positions.  Both are documented
    sensitivities; neither is an attribution.
    """
    from datetime import date as Date, timedelta

    if mode not in {"visitor_proportional", "equal_per_position"}:
        raise ValueError(f"unknown unattributed mode {mode!r}")
    positions = position_performance(data)
    weights: dict[tuple[str, str, str], dict[str, Fraction]] = defaultdict(dict)
    for performance in positions:
        key = (performance.studio, performance.cabinet, performance.layout)
        if mode == "visitor_proportional":
            weights[key][performance.sku] = weights[key].get(performance.sku, Fraction(0)) + performance.visitors
        else:
            weights[key][performance.sku] = Fraction(1)

    unmatched: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for checkout in data.raw["checkout"]:
        if checkout["take"] is None:
            unmatched[(checkout["sku"], checkout["date"])].append(checkout)

    split: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
    for (sku, checkout_date), rows in unmatched.items():
        previous = (Date.fromisoformat(checkout_date) - timedelta(days=1)).isoformat()
        candidates = []
        for operation in data.raw["operation"]:
            if operation["date"] != previous:
                continue
            for display in data.display_by_operation.get(operation["id"], []):
                if display["sku"] != sku:
                    continue
                key = (display["studio"], operation["cabinet"], operation["layout"])
                weight = weights.get(key, {}).get(sku, Fraction(0))
                if weight:
                    candidates.append((key, weight))
        total = sum(weight for _key, weight in candidates)
        if not total:
            continue
        amount = Fraction(sum(row["net"] for row in rows))
        for key, weight in candidates:
            split[key] += amount * weight / total
    return split


def studio_yield_table(
    data: DrillData,
    include_pending: bool = False,
    unattributed: str | None = None,
) -> list[dict[str, Any]]:
    """Per studio/cabinet/layout yield bands for plan trials.

    Ranges are labelled and are not additive across studios: the unconfirmed
    residual is a shared upper bound, so two studios on the same cabinet cannot
    each claim it in full at the same time.  ``include_pending`` and
    ``unattributed`` exist only for sensitivity views; defaults keep the strict
    linked-revenue position.
    """
    performances = position_performance(data)
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for performance in performances:
        key = (performance.studio, performance.cabinet, performance.layout)
        entry = grouped.setdefault(
            key,
            {
                "studio": performance.studio,
                "cabinet": performance.cabinet,
                "layout": performance.layout,
                "revenue_cents": 0,
                "linked_revenue_cents": 0,
                "pending_cents": 0,
                "takes": 0,
                "pending_takes": 0,
                "confirmed_ah": Fraction(0),
                "residual_ah": Fraction(0),
                "signed_ah": Fraction(0),
                "displayed_ah": Fraction(0),
                "exposure": set(),
                "visitor_observations": [],
            },
        )
        entry["revenue_cents"] += performance.revenue_cents
        entry["linked_revenue_cents"] += performance.revenue_cents
        entry["takes"] += performance.takes
        entry["pending_takes"] += performance.pending_takes
        entry["confirmed_ah"] += performance.confirmed_area_hours
        entry["residual_ah"] += performance.residual_area_hours
        entry["signed_ah"] += performance.signed_area_hours
        entry["displayed_ah"] += performance.displayed_area_hours
        entry["exposure"].add(performance.exposure)
        if performance.sku != NO_DISPLAY_SKU:
            entry["visitor_observations"].append({"sku": performance.sku, "visitors": performance.visitors})

    pending_mean = _pending_mean_scoped(data)
    for entry in grouped.values():
        key = (entry["studio"], entry["cabinet"], entry["layout"])
        pending = sum(
            (value for (studio, cabinet, layout, _sku), value in pending_mean.items() if (studio, cabinet, layout) == key),
            Fraction(0),
        )
        entry["pending_cents"] = pending
        if include_pending:
            entry["revenue_cents"] = Fraction(entry["revenue_cents"]) + pending
    if unattributed is not None:
        apartment = _unattributed_split(data, unattributed)
        for entry in grouped.values():
            key = (entry["studio"], entry["cabinet"], entry["layout"])
            entry["revenue_cents"] = Fraction(entry["revenue_cents"]) + apartment.get(key, Fraction(0))

    result: list[dict[str, Any]] = []
    for key in sorted(grouped):
        entry = grouped[key]
        wide = entry["confirmed_ah"] + entry["residual_ah"]
        low = None if wide == 0 else Fraction(entry["revenue_cents"]) / wide
        high = None
        if entry["confirmed_ah"] > 0:
            high = Fraction(entry["revenue_cents"] + entry["pending_cents"]) / entry["confirmed_ah"]
        elif wide > 0:
            high = Fraction(entry["revenue_cents"] + entry["pending_cents"]) / wide
        result.append(
            {
                "studio": entry["studio"],
                "cabinet": entry["cabinet"],
                "layout": entry["layout"],
                "revenue_cents": _num(entry["revenue_cents"]),
                "linked_revenue_cents": entry["linked_revenue_cents"],
                "pending_cents": _num(entry["pending_cents"]),
                "takes": entry["takes"],
                "pending_takes": entry["pending_takes"],
                "confirmed_area_hours": float(entry["confirmed_ah"]),
                "residual_area_hours": float(entry["residual_ah"]),
                "signed_area_hours": float(entry["signed_ah"]),
                "displayed_area_hours": float(entry["displayed_ah"]),
                "yield_low_cents_per_area_hour": None if low is None else float(low),
                "yield_high_cents_per_area_hour": None if high is None else float(high),
                "exposure": sorted(entry["exposure"]),
                "visitor_observations": entry["visitor_observations"],
                "note": "per-studio range; ranges are not additive across studios because the unconfirmed residual is shared",
            }
        )
    return result


def _num(value: int | Fraction) -> int | float:
    if isinstance(value, Fraction):
        return float(value) if value.denominator != 1 else int(value)
    return value
