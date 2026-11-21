"""Shared creative-retail cabinet analytics.

Public entry point: :func:`report`, plus the individual building blocks grouped
by concern:

``capacity``  sellable area-hours, holds, signed-share positions
``revenue``   pickup attribution, cashier reconciliation, yield bands
``analysis``  layout decomposition and bridge resolution
``plans``     next-quarter plan trials, promise gaps, notice impact
``trace``     month-end traceback from a metric to the raw rows
"""
from .model import DrillData, DataError, Issue
from .capacity import (
    cabinet_capacity_summary,
    next_capacity,
    operation_capacity,
    share_positions,
    studio_capacity_summary,
)
from .revenue import (
    build_ledger,
    pending_value_range,
    position_performance,
    studio_yield_table,
)
from .analysis import ambiguity_report, decompose_layouts, resolve_bridges
from .plans import evaluate_plan, notification_impact, plan_sensitivity, plan_summary, plan_trials
from .trace import trace_position, trace_promise, trace_take
from .report import report

__all__ = [
    "DrillData",
    "DataError",
    "Issue",
    "cabinet_capacity_summary",
    "next_capacity",
    "operation_capacity",
    "share_positions",
    "studio_capacity_summary",
    "build_ledger",
    "pending_value_range",
    "position_performance",
    "studio_yield_table",
    "ambiguity_report",
    "decompose_layouts",
    "resolve_bridges",
    "evaluate_plan",
    "notification_impact",
    "plan_sensitivity",
    "plan_summary",
    "plan_trials",
    "trace_position",
    "trace_promise",
    "trace_take",
    "report",
]
