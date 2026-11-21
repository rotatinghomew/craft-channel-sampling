"""One assembled monthly report, the same entry point for every question.

The report is JSON-serialisable and carries its own definitions, so a change in
methodology is visible in the output rather than implied by a number.
"""
from __future__ import annotations

from typing import Any

from .analysis import ambiguity_report, decompose_layouts
from .capacity import cabinet_capacity_summary, studio_capacity_summary
from .model import DrillData
from .plans import notification_impact, plan_sensitivity, plan_summary
from .revenue import build_ledger, pending_value_range, position_performance, studio_yield_table

DEFINITIONS = {
    "effective_cm2": "gross cm2 minus corridor and safety exclusions; never the gross figure",
    "area_hour": "effective cm2 multiplied by half-open clock hours that are open and actually sellable",
    "held_hour": "an hour inside a lecture or closure hold; excluded from every studio's opportunity, never treated as space that becomes free later",
    "signed_share": "confirmed_percent from the shares table; unconfirmed shares are reported as a range and never split evenly",
    "attribution": "a checkout carrying a take id is attributed to the take's cabinet, layout and date; delayed checkout or another cash desk does not move it",
    "unattributed": "a checkout without a take id; reported separately and reconciled to the cashier total, never spread",
    "pending": "a take without a linked checkout; identified but unsettled, not zero",
    "visitors": "observed per product display row; not independent footfall and never summed across studios",
    "yield": "linked revenue per signed area-hour; the low band uses the widest honest denominator, the high band adds a pending-pickup value band and uses only confirmed area-hours",
    "layout_change": "supported bridges only; an old cabinet with several candidates is excluded instead of merged",
    "plan_fee": "conservative fee income counts only met, feasible allocations; projected sales are separate",
}


def report(data: DrillData) -> dict[str, Any]:
    ledger = build_ledger(data)
    capacities = cabinet_capacity_summary(data)
    studios = studio_capacity_summary(data)
    positions = position_performance(data)
    yields = studio_yield_table(data)
    plans = plan_summary(data)
    sensitivity = plan_sensitivity(data)
    notices = notification_impact(data)
    bridges = ambiguity_report(data)
    decomposition = decompose_layouts(data)
    pending = pending_value_range(data)

    return {
        "source_issues": data.issue_dicts(),
        "definitions": DEFINITIONS,
        "capacity": {
            "by_cabinet_layout": capacities,
            "by_studio_cabinet": studios,
            "notes": [
                "open and held hours are reported separately so held time is never counted as opportunity",
                "unallocated display hours are opening hours with no product on show; they are opportunity, but not observed customer exposure",
            ],
        },
        "revenue": ledger.to_dict(),
        "unattributed": {
            "amount_cents": ledger.unattributed_cents,
            "reconciles": ledger.reconciles,
            "rows": ledger.unattributed_rows,
            "effect_on_comparison": "unattributed revenue is excluded from every plan and position; the comparison shows the ranking under strict attribution and under labelled sensitivities",
        },
        "pending_pickups": pending,
        "positions": [p.to_dict() for p in positions],
        "studio_yields": yields,
        "layout_change": {
            "decomposition": [d.to_dict() for d in decomposition],
            "bridges": bridges,
            "sensitivity_required": any(
                d["notes"] for d in (x.to_dict() for x in decomposition)
            ),
        },
        "plans": plans,
        "plan_sensitivity": sensitivity,
        "notifications": notices,
        "trace_hint": "use trace_take / trace_position / trace_promise with the ids printed in this report",
    }
