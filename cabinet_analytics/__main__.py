"""Command line entry point.

    python -m cabinet_analytics report   [--data PATH]
    python -m cabinet_analytics plan      [--data PATH] [--plan P1]
    python -m cabinet_analytics notices   [--data PATH]
    python -m cabinet_analytics trace-take TAKE_ID
    python -m cabinet_analytics trace-promise PLAN PROMISE_ID
    python -m cabinet_analytics trace-position STUDIO CABINET [SKU]

Results are pretty-printed UTF-8 JSON on stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .model import DrillData
from .plans import evaluate_plan, notification_impact, plan_sensitivity, plan_summary
from .report import report
from .trace import trace_position, trace_promise, trace_take

DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data" / "drill.json"


def _load(path: str) -> DrillData:
    data = DrillData.load(path)
    for issue in data.issues:
        print(f"[{issue.severity}] {issue.code}: {issue.detail}", file=sys.stderr)
    return data


def _emit(value) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cabinet_analytics", description=__doc__)
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="path to drill.json")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("report", help="full month-end report")
    plan_parser = sub.add_parser("plan", help="one plan trial or all plans")
    plan_parser.add_argument("--plan", help="plan id, e.g. P1")
    sub.add_parser("notices", help="post-signature lecture/closure impact")
    sub.add_parser("sensitivity", help="ranking sensitivity to unattributed revenue")
    take_parser = sub.add_parser("trace-take", help="walk a pickup to its records")
    take_parser.add_argument("take_id")
    promise_parser = sub.add_parser("trace-promise", help="walk a promise check to its records")
    promise_parser.add_argument("plan")
    promise_parser.add_argument("promise_id")
    position_parser = sub.add_parser("trace-position", help="walk a studio/cabinet position")
    position_parser.add_argument("studio")
    position_parser.add_argument("cabinet")
    position_parser.add_argument("sku", nargs="?")

    args = parser.parse_args(argv)
    data = _load(args.data)

    if args.command == "report":
        _emit(report(data))
    elif args.command == "plan":
        if args.plan:
            _emit(evaluate_plan(data, args.plan))
        else:
            _emit(plan_summary(data))
    elif args.command == "notices":
        _emit(notification_impact(data))
    elif args.command == "sensitivity":
        _emit(plan_sensitivity(data))
    elif args.command == "trace-take":
        _emit(trace_take(data, args.take_id))
    elif args.command == "trace-promise":
        _emit(trace_promise(data, args.plan, args.promise_id))
    elif args.command == "trace-position":
        _emit(trace_position(data, args.studio, args.cabinet, args.sku))
    else:  # pragma: no cover - argparse enforces choices
        parser.error(f"unknown command {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
