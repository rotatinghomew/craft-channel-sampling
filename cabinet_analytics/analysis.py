"""Period comparison, factor decomposition and layout-bridge resolution.

Decomposition
-------------
For each observed product position revenue is factored multiplicatively::

    revenue = signed area-hours
              x observed visitors per signed area-hour
              x pickups per observed visitor
              x net revenue per pickup

The four factors are physical opportunity, customer traffic, conversion and
basket value.  Effects between layouts are attributed with the Shapley value over
the four factors, so the four effects sum exactly to the revenue change and
interaction is spread across factors rather than dumped into the last one.
Visitors are per product observation, never independent footfall, so the
decomposition stays at product-position level.

Bridges
-------
A bridge row is only used when it is *supported* and gives a unique old-to-new
mapping.  Unsupported candidates are reported as an ambiguity; the old cabinet
is then excluded from cross-layout aggregation instead of being merged into a
guess.  This is deliberately conservative: S2 cannot be assigned to either S2
or S3.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from .model import DrillData
from .revenue import position_performance

FACTORS = ("opportunity", "traffic", "conversion", "settlement", "value")
FACTOR_LABELS = {
    "opportunity": "physical opportunity (signed area-hours)",
    "traffic": "observed visitors per signed area-hour",
    "conversion": "pickups per observed visitor",
    "settlement": "pickups with a linked checkout (data capture, not demand)",
    "value": "net revenue per linked pickup",
}


@dataclass
class BridgeResolution:
    unique: dict[str, str]
    ambiguous_old: dict[str, list[str]]
    unsupported_pairs: list[tuple[str, str]]
    supported_pairs: list[tuple[str, str]]
    complete: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "unique_map": self.unique,
            "ambiguous_old": self.ambiguous_old,
            "unsupported_pairs": [list(p) for p in self.unsupported_pairs],
            "supported_pairs": [list(p) for p in self.supported_pairs],
            "complete": self.complete,
            "note": self.note,
        }


def resolve_bridges(data: DrillData) -> BridgeResolution:
    """Use only supported bridges; an old cabinet with several targets is ambiguous."""
    supported: dict[str, set[str]] = defaultdict(set)
    unsupported: list[tuple[str, str]] = []
    supported_pairs: list[tuple[str, str]] = []
    for row in data.raw["bridges"]:
        if row["supported"]:
            supported[row["old_cab"]].add(row["new_cab"])
            supported_pairs.append((row["old_cab"], row["new_cab"]))
        else:
            unsupported.append((row["old_cab"], row["new_cab"]))
    unique: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    for old in {row["old_cab"] for row in data.raw["bridges"]}:
        targets = supported.get(old, set())
        if len(targets) == 1:
            unique[old] = next(iter(targets))
        else:
            candidates = set(targets) | {row["new_cab"] for row in data.raw["bridges"] if row["old_cab"] == old and not row["supported"]}
            ambiguous[old] = sorted(candidates)
    complete = not ambiguous
    note = (
        "all old cabinets map uniquely to a new cabinet"
        if complete
        else "not every old cabinet has a unique supported mapping; affected cabinets are excluded from cross-layout aggregation and must not be merged"
    )
    return BridgeResolution(unique=unique, ambiguous_old=ambiguous, unsupported_pairs=unsupported, supported_pairs=supported_pairs, complete=complete, note=note)


@dataclass
class FactorDecomposition:
    studio: str
    cabinet: str
    sku: str
    earlier_layout: str
    later_layout: str
    earlier_revenue_cents: int
    later_revenue_cents: int
    effects_cents: dict[str, int]
    effect_labels: dict[str, str]
    explained_cents: int
    residual_cents: int
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "studio": self.studio,
            "cabinet": self.cabinet,
            "sku": self.sku,
            "earlier_layout": self.earlier_layout,
            "later_layout": self.later_layout,
            "earlier_revenue_cents": self.earlier_revenue_cents,
            "later_revenue_cents": self.later_revenue_cents,
            "change_cents": self.later_revenue_cents - self.earlier_revenue_cents,
            "effects_cents": self.effects_cents,
            "effect_labels": self.effect_labels,
            "explained_cents": self.explained_cents,
            "residual_cents": self.residual_cents,
            "method": "Shapley attribution over five multiplicative factors; effects sum to the change",
            "notes": self.notes,
        }


def _factors(performance) -> dict[str, Fraction]:
    opportunity = Fraction(performance.signed_area_hours)
    visitors = Fraction(performance.visitors)
    takes = Fraction(performance.takes)
    linked = Fraction(performance.linked_takes)
    revenue = Fraction(performance.revenue_cents)
    if opportunity == 0:
        return {name: Fraction(0) for name in FACTORS}
    traffic = visitors / opportunity
    conversion = takes / visitors if visitors else Fraction(0)
    settlement = linked / takes if takes else Fraction(0)
    value = revenue / linked if linked else Fraction(0)
    return {"opportunity": opportunity, "traffic": traffic, "conversion": conversion, "settlement": settlement, "value": value}


def _product(factors: dict[str, Fraction], subset: Iterable[str]) -> Fraction:
    result = Fraction(1)
    for name in subset:
        result *= factors[name]
    return result


def _shapley(start: dict[str, Fraction], end: dict[str, Fraction]) -> dict[str, Fraction]:
    """Shapley value of each factor in a four-factor product game."""
    effects: dict[str, Fraction] = {name: Fraction(0) for name in FACTORS}

    def value(subset: frozenset[str]) -> Fraction:
        factors = {}
        for name in FACTORS:
            factors[name] = end[name] if name in subset else start[name]
        return _product(factors, FACTORS)

    for name in FACTORS:
        others = [f for f in FACTORS if f != name]
        for size in range(len(others) + 1):
            for subset in _combinations(others, size):
                weight = Fraction(_factorial(size) * _factorial(len(FACTORS) - size - 1), _factorial(len(FACTORS)))
                base = frozenset(subset)
                effects[name] += weight * (value(base | {name}) - value(base))
    return effects


def _settlement_rate(performance) -> Fraction:
    if performance.takes == 0:
        return Fraction(0)
    return Fraction(performance.linked_takes, performance.takes)


def _factorial(n: int) -> int:
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result


def _combinations(items: list[str], size: int):
    if size == 0:
        yield ()
        return
    for index in range(len(items)):
        for rest in _combinations(items[index + 1 :], size - 1):
            yield (items[index],) + rest


def decompose_layouts(data: DrillData) -> list[FactorDecomposition]:
    """Compare layouts per product position with a Shapley decomposition.

    Only cabinets with a unique supported bridge are compared; ambiguous old
    cabinets are left out and the reason is reported.
    """
    bridges = resolve_bridges(data)
    performances = position_performance(data)
    by_position: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(dict)
    for performance in performances:
        by_position[(performance.studio, performance.cabinet, performance.sku)][performance.layout] = performance

    results: list[FactorDecomposition] = []
    for key in sorted(by_position):
        layouts = by_position[key]
        if len(layouts) < 2:
            continue
        names = sorted(layouts)
        earlier, later = names[0], names[-1]
        first = layouts[earlier]
        second = layouts[later]
        if first.cabinet in bridges.ambiguous_old:
            continue
        if bridges.unique.get(first.cabinet, first.cabinet) != second.cabinet:
            continue
        start = _factors(first)
        end = _factors(second)
        shapley = _shapley(start, end)
        notes: list[str] = []
        if first.visitors == 0 or second.visitors == 0:
            notes.append("no observed visitors in one period; traffic and conversion cannot be separated, see the combined effect")
        if first.confirmed_area_hours and not second.confirmed_area_hours:
            notes.append(
                "the earlier layout's studio share was signed and the later one is unconfirmed; "
                "part of the opportunity effect is a change in signing status, not physical area. "
                "Confirm the share before treating the opportunity effect as a layout gain."
            )
        elif second.confirmed_area_hours and not first.confirmed_area_hours:
            notes.append(
                "the later layout's studio share is signed while the earlier one was unconfirmed; "
                "part of the opportunity effect is a change in signing status, not physical area."
            )
        if _settlement_rate(first) > _settlement_rate(second) and second.takes:
            notes.append("settlement capture fell between the layouts; the settlement effect reflects weaker data capture, not lower demand")
        elif _settlement_rate(first) < _settlement_rate(second) and first.takes:
            notes.append("settlement capture improved between the layouts; the settlement effect reflects data capture, not higher demand")
        if not second.takes:
            notes.append("no pickups in the later period; conversion and value are not identified and the change is carried by opportunity, traffic and settlement")
        total = sum(shapley.values(), Fraction(0))
        actual = Fraction(second.revenue_cents - first.revenue_cents)
        effects = {name: round(float(shapley[name])) for name in FACTORS}
        results.append(
            FactorDecomposition(
                studio=key[0],
                cabinet=first.cabinet,
                sku=key[2],
                earlier_layout=earlier,
                later_layout=later,
                earlier_revenue_cents=first.revenue_cents,
                later_revenue_cents=second.revenue_cents,
                effects_cents=effects,
                effect_labels=FACTOR_LABELS,
                explained_cents=round(float(total)),
                residual_cents=int(round(float(actual - total))),
                notes=notes,
            )
        )
    return results


def ambiguity_report(data: DrillData) -> dict[str, Any]:
    """State exactly what cannot be aggregated across the layout change."""
    bridges = resolve_bridges(data)
    old_cabinets = {row["cabinet"] for row in data.raw["operation"]}
    affected = sorted(old_cabinets & set(bridges.ambiguous_old))
    return {
        "bridges": bridges.to_dict(),
        "affected_old_cabinets": affected,
        "operations_excluded_from_cross_layout": sorted(
            row["id"] for row in data.raw["operation"] if row["cabinet"] in affected
        ),
        "positions_excluded": sorted(
            {f"{p.studio}:{p.cabinet}:{p.sku}" for p in position_performance(data) if p.cabinet in affected}
        ),
        "rule": "an old cabinet with no unique supported bridge is not merged into any new cabinet; its operations stay separate",
    }
