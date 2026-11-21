"""Plan trials for the next period: capacity, contract gaps and conservative income.

The leasing group asks for several layout/fee plans.  This module evaluates them
against the confirmed promises without relaxing any commitment:

* offered area-hours are ``allocated cm2 x hours actually available`` after
  lecture/closure holds, never the nominal opening hours;
* a plan that allocates more cm2 to one cabinet than the cabinet can sell is
  over capacity and its allocations cannot be added together as improvements;
* a studio is only moved to another cabinet when an explicit new signature
  exists; an allocation alone is not a re-signing and does not release the old
  committed position;
* a signed minimum block may not be promised to two studios in the same
  priority window; a shared cabinet without signed share percentages is reported
  as an unconfirmed sharing;
* conservative operating income counts only fees whose studio minimum is
  actually met and whose slot is feasible.  At-risk fees and projected sales are
  shown separately and are never added together.

Fields are integer cents, area in cm2, hours are half-open integers.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .capacity import available_intervals, effective_cm2
from .model import DrillData
from .timeutil import intersect, length

STATUS_MET = "met"
STATUS_MET_WITH_CONFLICT = "met_with_conflict"
STATUS_SHORT = "short"
STATUS_REASSIGNMENT_REQUIRED = "reassignment_required"
STATUS_NOT_ALLOCATED = "not_allocated"


@dataclass
class Allocation:
    plan: str
    cabinet: str
    studio: str
    date: str
    cm2: int
    fee_cents: int
    available_hours: int
    area_hours: int
    effective_cm2: int
    holds: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "cabinet": self.cabinet,
            "studio": self.studio,
            "date": self.date,
            "cm2": self.cm2,
            "fee_cents": self.fee_cents,
            "available_hours": self.available_hours,
            "area_hours": self.area_hours,
            "effective_cm2": self.effective_cm2,
            "holds": self.holds,
        }


@dataclass
class PromiseCheck:
    promise: str
    studio: str
    cabinet: str
    date: str
    min_cm2_hour: int
    confirmed_on: str
    allocated_cabinet: str | None
    allocated_cm2: int | None
    delivered_area_hours: int
    gap_area_hours: int
    proposed_elsewhere_area_hours: int
    status: str
    new_hold_notices: list[dict[str, Any]]
    shared_with: list[str]
    conflicts: list[str]
    reasons: list[str] = field(default_factory=list)

    @property
    def fee_is_safe(self) -> bool:
        return self.status == STATUS_MET

    def to_dict(self) -> dict[str, Any]:
        return {
            "promise": self.promise,
            "studio": self.studio,
            "cabinet": self.cabinet,
            "date": self.date,
            "min_cm2_hour": self.min_cm2_hour,
            "confirmed_on": self.confirmed_on,
            "allocated_cabinet": self.allocated_cabinet,
            "allocated_cm2": self.allocated_cm2,
            "delivered_area_hours": self.delivered_area_hours,
            "gap_area_hours": self.gap_area_hours,
            "proposed_elsewhere_area_hours": self.proposed_elsewhere_area_hours,
            "status": self.status,
            "new_hold_notices": self.new_hold_notices,
            "shared_with": self.shared_with,
            "conflicts": self.conflicts,
            "reasons": self.reasons,
        }


@dataclass
class PlanTrial:
    plan: str
    date: str
    allocations: list[Allocation]
    capacity: dict[str, dict[str, Any]]
    promise_checks: list[PromiseCheck]
    allocation_conflicts: list[dict[str, Any]]
    total_fee_cents: int
    committed_fee_cents: int
    new_fee_cents: int
    conservative_fee_cents: int
    at_risk_fee_cents: int
    projected_sales_low_cents: float | None
    projected_sales_high_cents: float | None
    sales_projection_notes: list[str]
    feasible: bool
    issues: list[str]

    @property
    def overlapping_improvements(self) -> bool:
        return any(entry["over_capacity"] for entry in self.capacity.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "date": self.date,
            "allocations": [a.to_dict() for a in self.allocations],
            "capacity": self.capacity,
            "promise_checks": [p.to_dict() for p in self.promise_checks],
            "allocation_conflicts": self.allocation_conflicts,
            "total_fee_cents": self.total_fee_cents,
            "committed_fee_cents": self.committed_fee_cents,
            "new_fee_cents": self.new_fee_cents,
            "conservative_fee_cents": self.conservative_fee_cents,
            "at_risk_fee_cents": self.at_risk_fee_cents,
            "projected_sales_low_cents": self.projected_sales_low_cents,
            "projected_sales_high_cents": self.projected_sales_high_cents,
            "sales_projection_notes": self.sales_projection_notes,
            "feasible": self.feasible,
            "overlapping_improvements": self.overlapping_improvements,
            "issues": self.issues,
            "rules": [
                "fees and projected sales are separate and are never added",
                "conservative income counts only met, feasible allocations",
                "over-capacity allocations are excluded, not summed",
                "held hours are excluded from deliverable hours",
                "a move needs a new signature; a plan does not release a committed position",
            ],
        }


def _hold_notices(data: DrillData, promise: dict[str, Any]) -> list[dict[str, Any]]:
    """Holds on the promised cabinet/date scheduled after the promise was signed."""
    notices = []
    for hold in data.holds_for(promise["cabinet"], promise["date"]):
        if hold["scheduled_on"] > promise["confirmed_on"]:
            overlap = length(intersect([(hold["from_hour"], hold["to_hour"])], (promise["from_hour"], promise["to_hour"])))
            if overlap:
                notices.append(
                    {
                        "cabinet": hold["cabinet"],
                        "date": hold["date"],
                        "from_hour": hold["from_hour"],
                        "to_hour": hold["to_hour"],
                        "kind": hold["kind"],
                        "scheduled_on": hold["scheduled_on"],
                        "overlap_hours": overlap,
                    }
                )
    return notices


def _plan_rows(data: DrillData, plan: str) -> list[dict[str, Any]]:
    return [row for row in data.raw["candidate"] if row["plan"] == plan]


def evaluate_plan(data: DrillData, plan: str) -> PlanTrial:
    rows = _plan_rows(data, plan)
    if not rows:
        raise ValueError(f"unknown plan {plan!r}")
    day = rows[0]["date"]
    allocations: list[Allocation] = []
    capacity: dict[str, dict[str, Any]] = {}
    issues: list[str] = []

    for cabinet in sorted({row["cabinet"] for row in rows}):
        layout_rows = data.next_layout_by_cabinet_date.get((cabinet, day), [])
        if not layout_rows:
            issues.append(f"plan {plan}: no next layout for {cabinet} on {day}")
            continue
        layout = layout_rows[0]
        window = (layout["from_hour"], layout["to_hour"])
        effective = effective_cm2(layout)
        available = available_intervals(data, cabinet, day, window)
        available_hours = length(available)
        holds = data.holds_for(cabinet, day)
        cabinet_rows = [row for row in rows if row["cabinet"] == cabinet]
        allocated = sum(row["cm2"] for row in cabinet_rows)
        capacity[cabinet] = {
            "layout": layout["layout"],
            "effective_cm2": effective,
            "allocated_cm2": allocated,
            "free_cm2": effective - allocated,
            "open_hours": window[1] - window[0],
            "available_hours": available_hours,
            "available_windows": [list(i) for i in available],
            "holds": holds,
            "studios": sorted({row["studio"] for row in cabinet_rows}),
            "over_capacity": allocated > effective,
        }
        if allocated > effective:
            issues.append(f"plan {plan}: {cabinet} allocates {allocated} cm2 above {effective} cm2 effective; allocations overlap and cannot be summed as gains")
        if available_hours == 0:
            issues.append(f"plan {plan}: {cabinet} has no sellable hours on {day} after holds; any fee there is not deliverable")
        for row in cabinet_rows:
            allocations.append(
                Allocation(
                    plan=plan,
                    cabinet=cabinet,
                    studio=row["studio"],
                    date=day,
                    cm2=row["cm2"],
                    fee_cents=row["fee"],
                    available_hours=available_hours,
                    area_hours=row["cm2"] * available_hours,
                    effective_cm2=effective,
                    holds=holds,
                )
            )

    # A cabinet offered to more than one studio in the same priority window is a
    # conflict when any of them holds a signed minimum there; otherwise the plan
    # still needs signed share percentages before it can be called agreed.
    allocation_conflicts: list[dict[str, Any]] = []
    for cabinet, entry in capacity.items():
        if len(entry["studios"]) < 2:
            continue
        layout = data.next_layout_by_cabinet_date.get((cabinet, day), [{}])[0]
        window = (layout.get("from_hour", 9), layout.get("to_hour", 17))
        signed = [
            promise["studio"]
            for promise in data.raw["promise"]
            if promise["cabinet"] == cabinet
            and promise["date"] == day
            and length(intersect([(promise["from_hour"], promise["to_hour"])], window)) > 0
        ]
        conflict = {
            "cabinet": cabinet,
            "studios": entry["studios"],
            "signed_studios": sorted(set(signed) & set(entry["studios"])),
            "code": "priority_slot_promised_to_multiple_studios" if set(signed) & set(entry["studios"]) else "sharing_needs_signed_shares",
            "detail": (
                "the same cabinet's priority window is allocated to several studios; a signed minimum block cannot be promised twice"
                if set(signed) & set(entry["studios"])
                else "several studios share this cabinet but the plan supplies no signed share percentages; cm2 must not be added as independent opportunities"
            ),
        }
        allocation_conflicts.append(conflict)
        issues.append(f"plan {plan}: {conflict['code']} on {cabinet} for {', '.join(entry['studios'])}")

    promised_cabinets = {(row["studio"], row["cabinet"]) for row in data.raw["promise"]}
    promise_checks: list[PromiseCheck] = []
    for promise in [row for row in data.raw["promise"] if row["date"] == day]:
        studio = promise["studio"]
        studio_allocations = [row for row in allocations if row.studio == studio]
        on_signed = [row for row in studio_allocations if row.cabinet == promise["cabinet"]]
        elsewhere = [row for row in studio_allocations if row.cabinet != promise["cabinet"]]
        reasons: list[str] = []
        conflicts: list[str] = []
        anchor_cabinet = on_signed[0].cabinet if on_signed else (elsewhere[0].cabinet if elsewhere else "")
        shared_with = sorted(
            row.studio for row in allocations if row.cabinet == anchor_cabinet and row.studio != studio
        )
        notices = _hold_notices(data, promise)

        if not studio_allocations:
            status = STATUS_NOT_ALLOCATED
            allocated_cabinet = None
            allocated_cm2 = None
            delivered = 0
            proposed_elsewhere = 0
            reasons.append("no allocation for this studio in the plan; the signed position remains with the studio")
        elif not on_signed:
            allocation = elsewhere[0]
            status = STATUS_REASSIGNMENT_REQUIRED
            allocated_cabinet = allocation.cabinet
            allocated_cm2 = allocation.cm2
            delivered = 0
            proposed_elsewhere = allocation.area_hours
            reasons.append(
                f"studio is proposed for {allocation.cabinet}, but the signed promise is on {promise['cabinet']}; "
                "a move needs an explicit new signature and the plan alone does not release the old position"
            )
        else:
            allocation = on_signed[0]
            allocated_cabinet = allocation.cabinet
            allocated_cm2 = allocation.cm2
            delivered = allocation.area_hours
            proposed_elsewhere = sum(row.area_hours for row in elsewhere)
            conflict_for_cabinet = next((c for c in allocation_conflicts if c["cabinet"] == promise["cabinet"]), None)
            if delivered >= promise["min_cm2_hour"] and conflict_for_cabinet is None:
                status = STATUS_MET
            elif delivered >= promise["min_cm2_hour"]:
                status = STATUS_MET_WITH_CONFLICT
                conflicts.append(conflict_for_cabinet["code"])
                reasons.append("the minimum is numerically covered, but the same priority slot is offered to another studio; treat the fee as at risk")
            else:
                status = STATUS_SHORT
                reasons.append(f"delivers {delivered} area-hours against a signed minimum of {promise['min_cm2_hour']}")
            if len(on_signed) > 1:
                conflicts.append("multiple_allocations_on_signed_cabinet")
                reasons.append("studio has more than one allocation on the signed cabinet; verify the combined cm2 against the effective area")
            if elsewhere:
                reasons.append("additional allocations in other cabinets are not credited to the signed minimum")

        gap = max(0, promise["min_cm2_hour"] - delivered) if status != STATUS_REASSIGNMENT_REQUIRED else promise["min_cm2_hour"]
        if notices and gap > 0:
            reasons.append("new lecture/closure notice after confirmation; the committed minimum is not reduced, so the shortfall must be negotiated")
        elif notices:
            reasons.append("new lecture/closure notice after confirmation removes hours, but the allocated cm2 still covers the signed minimum after the notice")
        promise_checks.append(
            PromiseCheck(
                promise=promise["id"],
                studio=studio,
                cabinet=promise["cabinet"],
                date=promise["date"],
                min_cm2_hour=promise["min_cm2_hour"],
                confirmed_on=promise["confirmed_on"],
                allocated_cabinet=allocated_cabinet,
                allocated_cm2=allocated_cm2,
                delivered_area_hours=delivered,
                gap_area_hours=gap,
                proposed_elsewhere_area_hours=proposed_elsewhere,
                status=status,
                new_hold_notices=notices,
                shared_with=shared_with,
                conflicts=conflicts,
                reasons=reasons,
            )
        )

    yields = {(row["studio"], row["cabinet"], row["layout"]): row for row in _yield_table(data)}
    total_fee = sum(row["fee"] for row in rows)
    committed_fee = 0
    new_fee = 0
    sales_low = 0.0
    sales_high = 0.0
    notes: list[str] = []
    seen_notes: set[str] = set()

    promised_cabinets_by_studio: dict[str, str] = {row["studio"]: row["cabinet"] for row in data.raw["promise"] if row["date"] == day}
    safe_checks = {(check.studio, check.allocated_cabinet) for check in promise_checks if check.fee_is_safe}
    for allocation in allocations:
        cabinet_capacity = capacity.get(allocation.cabinet, {})
        signed_here = promised_cabinets_by_studio.get(allocation.studio) == allocation.cabinet
        signed_elsewhere = allocation.studio in promised_cabinets_by_studio and not signed_here

        if signed_here:
            if (allocation.studio, allocation.cabinet) in safe_checks:
                committed_fee += allocation.fee_cents
            # otherwise the signed minimum is not met or the slot is conflicted: at risk
        elif signed_elsewhere:
            note = f"{allocation.studio}'s signed position is {promised_cabinets_by_studio[allocation.studio]}, not {allocation.cabinet}; the fee is contingent on a new signature"
            if note not in seen_notes:
                notes.append(note)
                seen_notes.add(note)
        elif cabinet_capacity.get("over_capacity") or cabinet_capacity.get("available_hours", 0) == 0:
            note = f"{allocation.cabinet} is infeasible in {plan}; {allocation.studio}'s fee is at risk rather than counted"
            if note not in seen_notes:
                notes.append(note)
                seen_notes.add(note)
        elif len(cabinet_capacity.get("studios", [])) > 1:
            note = f"{allocation.cabinet} is shared in {plan} without signed share percentages; {allocation.studio}'s fee is at risk"
            if note not in seen_notes:
                notes.append(note)
                seen_notes.add(note)
        else:
            new_fee += allocation.fee_cents

        # Sales are projected only from a feasible slot; an over-capacity
        # allocation is never a gain because two studios cannot both use it.
        if cabinet_capacity.get("over_capacity") or cabinet_capacity.get("available_hours", 0) == 0:
            note = f"{allocation.cabinet} is infeasible in {plan}; {allocation.studio}'s sales are not projected"
            if note not in seen_notes:
                notes.append(note)
                seen_notes.add(note)
            continue

        key = (allocation.studio, allocation.cabinet, "V2")
        entry = yields.get(key)
        if entry is None:
            note = f"no historical V2 yield for {allocation.studio}/{allocation.cabinet}; sales not projected"
            if note not in seen_notes:
                notes.append(note)
                seen_notes.add(note)
            continue
        low = entry["yield_low_cents_per_area_hour"]
        high = entry["yield_high_cents_per_area_hour"]
        if low is not None:
            sales_low += low * allocation.area_hours
        if high is not None:
            sales_high += high * allocation.area_hours

    conservative_fee = committed_fee + new_fee
    feasible = (
        not any(entry["over_capacity"] for entry in capacity.values())
        and not allocation_conflicts
        and all(check.status == STATUS_MET for check in promise_checks)
        and not issues
    )
    return PlanTrial(
        plan=plan,
        date=day,
        allocations=allocations,
        capacity=capacity,
        promise_checks=promise_checks,
        allocation_conflicts=allocation_conflicts,
        total_fee_cents=total_fee,
        committed_fee_cents=committed_fee,
        new_fee_cents=new_fee,
        conservative_fee_cents=conservative_fee,
        at_risk_fee_cents=total_fee - conservative_fee,
        projected_sales_low_cents=sales_low if sales_low else None,
        projected_sales_high_cents=sales_high if sales_high else None,
        sales_projection_notes=notes,
        feasible=feasible,
        issues=issues,
    )


def _yield_table(data: DrillData, **kwargs: Any) -> list[dict[str, Any]]:
    from .revenue import studio_yield_table

    return studio_yield_table(data, **kwargs)


def plan_trials(data: DrillData) -> list[PlanTrial]:
    return [evaluate_plan(data, plan) for plan in sorted({row["plan"] for row in data.raw["candidate"]})]


def plan_summary(data: DrillData) -> dict[str, Any]:
    """Compare plans without adding overlapping improvements."""
    trials = plan_trials(data)
    return {
        "plans": [trial.to_dict() for trial in trials],
        "conservative_fee_ranking": [t.plan for t in sorted(trials, key=lambda t: (-t.conservative_fee_cents, t.plan))],
        "feasible_plans": [t.plan for t in trials if t.feasible],
        "rules": [
            "conservative fee income counts only met, feasible allocations",
            "projected sales are separate from fees and are never added to them",
            "overlapping allocations on one cabinet are excluded instead of summed",
            "held hours are excluded from deliverable hours",
            "a studio keeps its signed cabinet until an explicit re-signing exists",
        ],
    }


# ------------------------------------------------------------------ sensitivity


def plan_sensitivity(data: DrillData) -> dict[str, Any]:
    """Recompute plan sales under alternative, documented treatments of unattributed revenue.

    The alternatives are explicitly *not* attribution.  They exist to show
    whether a defensible treatment of unattributable customers changes the
    ranking, which is what the leasing group must see before using the order.
    """
    trials = plan_trials(data)
    views = {
        "linked_only": _yield_table(data),
        "pending_at_mean": _yield_table(data, include_pending=True),
        "unattributed_by_visitors": _yield_table(data, unattributed="visitor_proportional"),
        "unattributed_by_equal": _yield_table(data, unattributed="equal_per_position"),
    }
    sales: dict[str, dict[str, float]] = {}
    for view, table in views.items():
        by_key = {(row["studio"], row["cabinet"], row["layout"]): row for row in table}
        per_plan: dict[str, float] = {}
        for trial in trials:
            total = 0.0
            for allocation in trial.allocations:
                if trial.capacity.get(allocation.cabinet, {}).get("over_capacity"):
                    continue
                entry = by_key.get((allocation.studio, allocation.cabinet, "V2"))
                if entry is None or entry["yield_low_cents_per_area_hour"] is None:
                    continue
                total += entry["yield_low_cents_per_area_hour"] * allocation.area_hours
            per_plan[trial.plan] = total
        sales[view] = per_plan
    rankings = {
        view: [plan for plan, _value in sorted(per_plan.items(), key=lambda item: (-item[1], item[0]))]
        for view, per_plan in sales.items()
    }
    baseline = rankings["linked_only"]
    flips = {view: ranking != baseline for view, ranking in rankings.items()}
    return {
        "sales_by_view": sales,
        "rankings": rankings,
        "ranking_flips_vs_linked_only": flips,
        "any_flip": any(flips.values()),
        "assumptions": {
            "linked_only": "only checkouts carrying a take id; no pending or unattributed revenue",
            "pending_at_mean": "identified unsettled pickups valued at the observed mean net of their sku",
            "unattributed_by_visitors": "unattributed checkouts spread over the preceding day's same-sku positions in proportion to observed visitors",
            "unattributed_by_equal": "unattributed checkouts spread equally over the preceding day's same-sku positions",
        },
        "note": "every non-linked view is a labelled sensitivity, not evidence; it never moves money in the ledger or the fee trial",
    }


# ------------------------------------------------------------------- notices


def notification_impact(data: DrillData) -> dict[str, Any]:
    """List commitments affected by post-confirmation lecture/closure notices.

    The promised minimum is left untouched.  The report names the studios that
    must negotiate make-up hours, the shortfall, and the remaining opportunity
    that can be adjusted without touching any signed minimum.  A released slot
    moves to another studio only after an explicit new signature.
    """
    affected = []
    for promise in data.raw["promise"]:
        notices = _hold_notices(data, promise)
        if not notices:
            continue
        available_hours = length(
            available_intervals(
                data,
                promise["cabinet"],
                promise["date"],
                (promise["from_hour"], promise["to_hour"]),
            )
        )
        affected.append(
            {
                "promise": promise["id"],
                "studio": promise["studio"],
                "cabinet": promise["cabinet"],
                "date": promise["date"],
                "min_cm2_hour_unchanged": promise["min_cm2_hour"],
                "available_hours_after_notice": available_hours,
                "notices": notices,
                "action": "negotiate make-up for this studio; the committed minimum is not reduced",
                "released_to_another_studio_without_signature": False,
            }
        )

    adjustable: dict[str, list[dict[str, Any]]] = {}
    committed_by_cabinet: dict[str, list[str]] = defaultdict(list)
    for promise in data.raw["promise"]:
        committed_by_cabinet[promise["cabinet"]].append(promise["studio"])
    for cabinet in sorted(committed_by_cabinet):
        layout_rows = data.next_layout_by_cabinet_date.get(
            (cabinet, next((p["date"] for p in data.raw["promise"] if p["cabinet"] == cabinet), "")),
            [],
        )
        if not layout_rows:
            continue
        layout = layout_rows[0]
        promised_date = next(p["date"] for p in data.raw["promise"] if p["cabinet"] == cabinet)
        available = available_intervals(data, cabinet, promised_date, (layout["from_hour"], layout["to_hour"]))
        adjustable[cabinet] = [
            {
                "date": promised_date,
                "from_hour": start,
                "to_hour": end,
                "hours": end - start,
                "note": "open hours not removed by a hold; adjustable only after the signed minimum is met and only after an explicit new signature for another studio",
            }
            for start, end in available
        ]
    return {
        "affected_commitments": affected,
        "adjustable_remaining_opportunity": adjustable,
        "committed_studios_by_cabinet": {k: sorted(v) for k, v in sorted(committed_by_cabinet.items())},
        "rules": [
            "post-confirmation notices do not change the promised minimum",
            "held hours are never treated as space that will be free later",
            "a released position moves to another studio only after an explicit new signature",
            "the remaining adjustable opportunity excludes every hour inside a hold",
        ],
    }
