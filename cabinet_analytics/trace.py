"""Month-end traceback: from a yield or a promise gap to the raw records.

Every number in the report can be walked back to source rows.  This module
produces those chains explicitly, including which layout version and which
operation record a take belongs to, which holds affected the hour, and what was
actually signed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .analysis import resolve_bridges
from .capacity import available_intervals
from .model import DrillData
from .plans import evaluate_plan
from .revenue import build_ledger, position_performance
from .timeutil import length


@dataclass
class TraceResult:
    subject: str
    rows: dict[str, Any]
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "rows": self.rows, "findings": self.findings}


def trace_take(data: DrillData, take_id: str) -> TraceResult:
    take = data.takes.get(take_id)
    if take is None:
        return TraceResult(subject=f"take:{take_id}", rows={}, findings=[f"no take {take_id} in source"])
    operation = data.operations.get(take["operation"])
    ledger = build_ledger(data)
    studio = data.studio_for(take["operation"], take["sku"])
    checkout = ledger.by_take.get(take_id)
    display = [row for row in data.display_by_operation.get(take["operation"], []) if row["sku"] == take["sku"]]
    share = [row for row in data.shares_by_operation.get(take["operation"], []) if row["studio"] == studio]
    holds = [
        row
        for row in data.holds_for(operation["cabinet"], operation["date"])
        if row["from_hour"] <= take["hour"] < row["to_hour"]
    ] if operation else []
    bridges = resolve_bridges(data)
    findings: list[str] = []
    if checkout is None:
        findings.append("pickup has no linked checkout: revenue is identified but unsettled, not zero")
    elif checkout["cash_desk"] != operation["cabinet"]:
        findings.append(f"checked out at {checkout['cash_desk']} although the pickup was at {operation['cabinet']}; revenue stays with the pickup cabinet")
    if checkout and checkout["checkout_date"] != take["date"]:
        findings.append(f"checkout on {checkout['checkout_date']} is delayed from pickup on {take['date']}; revenue stays on the pickup date")
    if holds:
        findings.append("pickup fell inside a held hour; the hour is excluded from display opportunity")
    if operation and operation["cabinet"] in bridges.ambiguous_old:
        findings.append(f"{operation['cabinet']} has more than one new-cabinet candidate; this position is not merged across the layout change")
    return TraceResult(
        subject=f"take:{take_id}",
        rows={
            "take": take,
            "operation": operation,
            "layout": operation["layout"] if operation else None,
            "display": display,
            "share": share,
            "holds": holds,
            "checkout": checkout,
            "bridge": bridges.to_dict(),
        },
        findings=findings,
    )


def trace_position(data: DrillData, studio: str, cabinet: str, sku: str | None = None) -> TraceResult:
    performances = [
        p.to_dict()
        for p in position_performance(data)
        if p.studio == studio and p.cabinet == cabinet and (sku is None or p.sku == sku)
    ]
    ledger = build_ledger(data)
    take_ids = sorted(
        take_id
        for take_id, row in ledger.by_take.items()
        if row["studio"] == studio and row["cabinet"] == cabinet and (sku is None or row["sku"] == sku)
    )
    pending = [
        row for row in ledger.pending_take_rows if row["studio"] == studio and row["cabinet"] == cabinet and (sku is None or row["sku"] == sku)
    ]
    operations = [
        row for row in data.raw["operation"] if row["cabinet"] == cabinet and (sku is None or True)
    ]
    findings: list[str] = []
    if any(p["exposure"] == "held_only" for p in performances):
        findings.append("all display hours for this position were held; low revenue is not evidence of low demand")
    if any(p["residual_area_hours"] > 0 for p in performances):
        findings.append("part of this position's opportunity is unconfirmed; the upper yield band uses a shared residual and is not additive with other studios")
    return TraceResult(
        subject=f"position:{studio}:{cabinet}:{sku or '*'}",
        rows={
            "performance": performances,
            "linked_take_ids": take_ids,
            "pending_take_rows": pending,
            "operations": operations[:3],
            "operation_count": len(operations),
            "shares": [
                row for row in data.raw["shares"] if row["operation"] in {o["id"] for o in operations} and row["studio"] == studio
            ],
        },
        findings=findings,
    )


def trace_promise(data: DrillData, plan: str, promise_id: str) -> TraceResult:
    trial = evaluate_plan(data, plan)
    check = next((c for c in trial.promise_checks if c.promise == promise_id), None)
    if check is None:
        return TraceResult(subject=f"promise:{promise_id}", rows={}, findings=[f"promise {promise_id} is not on {plan}'s date"])
    promise = next(row for row in data.raw["promise"] if row["id"] == promise_id)
    layout = data.next_layout_by_cabinet_date.get((promise["cabinet"], promise["date"]), [])
    findings: list[str] = []
    if check.gap_area_hours:
        findings.append(f"gap of {check.gap_area_hours} area-hours must be negotiated; the signed minimum is not lowered")
    if check.new_hold_notices:
        findings.append("a notice scheduled after signature removes hours; those hours are not treated as free later")
    return TraceResult(
        subject=f"promise:{promise_id}:{plan}",
        rows={
            "promise": promise,
            "allocation": next((a.to_dict() for a in trial.allocations if a.studio == check.studio and a.cabinet == check.allocated_cabinet), None),
            "check": check.to_dict(),
            "next_layout": layout,
            "holds_on_cabinet": data.holds_for(promise["cabinet"], promise["date"]),
            "available_hours_after_notices": length(
                available_intervals(data, promise["cabinet"], promise["date"], (promise["from_hour"], promise["to_hour"]))
            ),
        },
        findings=findings,
    )
