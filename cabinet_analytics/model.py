"""Typed access to the drill tables and data-integrity checks.

The source is a UTF-8 JSON object whose top-level keys are table names.  This
module keeps the raw rows, builds lookup indexes, and collects *issues* instead
of silently repairing facts.  Nothing here invents a value for a missing fact:
``null`` stays missing and is reported as such.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date as Date
from pathlib import Path
from typing import Any

from .timeutil import Interval, merge, overlap

TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "operation": ("id", "cabinet", "layout", "date", "from_hour", "to_hour", "gross_cm2", "exclusion_cm2"),
    "shares": ("operation", "studio", "from_hour", "to_hour", "confirmed_percent"),
    "display": ("operation", "sku", "studio", "from_hour", "to_hour", "visitors"),
    "take": ("id", "operation", "sku", "hour", "date"),
    "checkout": ("id", "take", "sku", "cash_desk", "date", "net"),
    "holds": ("cabinet", "date", "from_hour", "to_hour", "kind", "scheduled_on"),
    "bridges": ("old_cab", "new_cab", "supported"),
    "next_layout": ("cabinet", "layout", "date", "from_hour", "to_hour", "gross_cm2", "exclusion_cm2"),
    "promise": ("id", "studio", "cabinet", "date", "from_hour", "to_hour", "min_cm2_hour", "confirmed_on"),
    "candidate": ("plan", "cabinet", "studio", "date", "from_hour", "to_hour", "cm2", "fee"),
}


class DataError(ValueError):
    """Structural problem that prevents any analysis."""


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str  # "error" | "warning" | "info"
    detail: str
    ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail, "ids": list(self.ids)}


def _hours(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataError(f"{field_name} must be an integer hour, got {value!r}")
    return value


def _cents(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataError(f"{field_name} must be integer cents, got {value!r}")
    return value


def _iso(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise DataError(f"{field_name} must be an ISO date string, got {value!r}")
    try:
        Date.fromisoformat(value)
    except ValueError as exc:
        raise DataError(f"{field_name} is not an ISO date: {value!r}") from exc
    return value


@dataclass
class DrillData:
    raw: dict[str, list[dict[str, Any]]]
    issues: list[Issue] = field(default_factory=list)

    # indexes built in __post_init__
    operations: dict[str, dict[str, Any]] = field(init=False)
    takes: dict[str, dict[str, Any]] = field(init=False)
    checkouts: dict[str, dict[str, Any]] = field(init=False)
    shares_by_operation: dict[str, list[dict[str, Any]]] = field(init=False)
    display_by_operation: dict[str, list[dict[str, Any]]] = field(init=False)
    shares_by_studio: dict[tuple[str, str], list[dict[str, Any]]] = field(init=False)
    operations_by_cabinet_date: dict[tuple[str, str], list[dict[str, Any]]] = field(init=False)
    holds_by_cabinet_date: dict[tuple[str, str], list[dict[str, Any]]] = field(init=False)
    checkouts_by_take: dict[str, list[dict[str, Any]]] = field(init=False)
    next_layout_by_cabinet_date: dict[tuple[str, str], list[dict[str, Any]]] = field(init=False)

    def __post_init__(self) -> None:
        self.operations = self._unique("operation")
        self.takes = self._unique("take")
        self.checkouts = self._unique("checkout")
        self.shares_by_operation = self._group("shares", "operation")
        self.display_by_operation = self._group("display", "operation")
        self.operations_by_cabinet_date = {
            key: rows for key, rows in self._group("operation", ("cabinet", "date")).items()
        }
        self.holds_by_cabinet_date = {
            key: rows for key, rows in self._group("holds", ("cabinet", "date")).items()
        }
        self.checkouts_by_take = {
            take_id: rows
            for take_id, rows in self._group("checkout", "take", skip_none=True).items()
        }
        self.next_layout_by_cabinet_date = {
            key: rows for key, rows in self._group("next_layout", ("cabinet", "date")).items()
        }
        self.shares_by_studio = defaultdict(list)
        for operation_id, rows in self.shares_by_operation.items():
            for row in rows:
                self.shares_by_studio[(operation_id, row["studio"])].append(row)
        self._validate()

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | Path) -> "DrillData":
        import json

        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"cannot read {source}: {exc}") from exc
        return cls.from_raw(raw)

    @classmethod
    def from_raw(cls, raw: Any) -> "DrillData":
        if not isinstance(raw, dict):
            raise DataError("top level must be a JSON object of table -> rows")
        missing = set(TABLE_FIELDS) - set(raw)
        if missing:
            raise DataError(f"missing tables: {sorted(missing)}")
        extra = set(raw) - set(TABLE_FIELDS)
        if extra:
            raise DataError(f"unknown tables: {sorted(extra)}")
        for table, fields in TABLE_FIELDS.items():
            rows = raw[table]
            if not isinstance(rows, list):
                raise DataError(f"{table} must be a list of rows")
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or set(row) != set(fields):
                    raise DataError(f"{table}[{index}] fields must be exactly {list(fields)}")
        return cls(raw={table: [dict(row) for row in rows] for table, rows in raw.items()})

    # ------------------------------------------------------------------ helpers
    def _unique(self, table: str) -> dict[str, dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for row in self.raw[table]:
            key = row["id"]
            if key in seen:
                self.issues.append(Issue("duplicate_id", "error", f"duplicate id in {table}", (str(key),)))
            seen[key] = row
        return seen

    def _group(self, table: str, keys: str | tuple[str, ...], skip_none: bool = False) -> dict[Any, list[dict[str, Any]]]:
        key_names = (keys,) if isinstance(keys, str) else keys
        grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for row in self.raw[table]:
            values = tuple(row[k] for k in key_names)
            if skip_none and values[0] is None:
                continue
            grouped[values if len(values) > 1 else values[0]].append(row)
        return dict(grouped)

    def operation_for(self, take: dict[str, Any]) -> dict[str, Any] | None:
        return self.operations.get(take["operation"])

    def studio_for(self, operation_id: str, sku: str) -> str | None:
        matches = [row["studio"] for row in self.display_by_operation.get(operation_id, ()) if row["sku"] == sku]
        if len(matches) != 1:
            return None
        return matches[0]

    def holds_for(self, cabinet: str, day: str) -> list[dict[str, Any]]:
        return self.holds_by_cabinet_date.get((cabinet, day), [])

    def hold_intervals(self, cabinet: str, day: str) -> list[Interval]:
        return merge([(row["from_hour"], row["to_hour"]) for row in self.holds_for(cabinet, day)])

    def operation_window(self, operation: dict[str, Any]) -> Interval:
        return (operation["from_hour"], operation["to_hour"])

    def display_windows(self, operation_id: str, studio: str | None = None) -> list[Interval]:
        rows = self.display_by_operation.get(operation_id, ())
        if studio is not None:
            rows = [row for row in rows if row["studio"] == studio]
        return merge([(row["from_hour"], row["to_hour"]) for row in rows])

    # ------------------------------------------------------------- validation
    def _validate(self) -> None:
        for operation in self.raw["operation"]:
            _hours(operation["from_hour"], "operation.from_hour")
            _hours(operation["to_hour"], "operation.to_hour")
            _iso(operation["date"], "operation.date")
            _cents(operation["gross_cm2"], "operation.gross_cm2")
            _cents(operation["exclusion_cm2"], "operation.exclusion_cm2")
            if operation["to_hour"] < operation["from_hour"]:
                self.issues.append(Issue("inverted_operation_window", "error", f"operation {operation['id']} ends before it starts", (operation["id"],)))
            if operation["gross_cm2"] < 0 or operation["exclusion_cm2"] < 0:
                self.issues.append(Issue("negative_area", "error", f"operation {operation['id']} has negative area", (operation["id"],)))
            if operation["gross_cm2"] - operation["exclusion_cm2"] <= 0:
                self.issues.append(Issue("non_positive_effective_area", "warning", f"operation {operation['id']} has no positive sellable area", (operation["id"],)))

        for key, rows in self.operations_by_cabinet_date.items():
            if len(rows) > 1:
                spans = [(row["from_hour"], row["to_hour"]) for row in rows]
                if any(overlap(a, b) for a in spans for b in spans if a != b):
                    self.issues.append(Issue("overlapping_operations", "error", f"cabinet {key[0]} on {key[1]} has overlapping operation windows", tuple(row["id"] for row in rows)))

        percent_by_operation: dict[str, int] = defaultdict(int)
        for share in self.raw["shares"]:
            _hours(share["from_hour"], "shares.from_hour")
            _hours(share["to_hour"], "shares.to_hour")
            if share["confirmed_percent"] is not None:
                value = share["confirmed_percent"]
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                    self.issues.append(Issue("bad_share_percent", "error", f"share percent must be 0..100 or null, got {value!r}", (share["operation"],)))
                else:
                    percent_by_operation[share["operation"]] += value
            if share["operation"] not in self.operations:
                self.issues.append(Issue("share_unknown_operation", "error", "share references an unknown operation", (share["operation"],)))
        for operation_id, total in percent_by_operation.items():
            if total > 100:
                self.issues.append(Issue("share_percent_over_100", "error", f"confirmed shares total {total}% for {operation_id}", (operation_id,)))

        for row in self.raw["display"]:
            _hours(row["from_hour"], "display.from_hour")
            _hours(row["to_hour"], "display.to_hour")
            _cents(row["visitors"], "display.visitors")
            if row["operation"] not in self.operations:
                self.issues.append(Issue("display_unknown_operation", "error", "display references an unknown operation", (row["operation"],)))
            if not any(s["studio"] == row["studio"] for s in self.shares_by_operation.get(row["operation"], ())):
                self.issues.append(Issue("display_without_share", "warning", f"studio {row['studio']} is displayed but has no share record", (row["operation"],)))
            window = self.operation_window(self.operations[row["operation"]]) if row["operation"] in self.operations else None
            if window and (row["from_hour"] > row["to_hour"] or row["from_hour"] < window[0] or row["to_hour"] > window[1]):
                self.issues.append(Issue("display_outside_operation", "warning", f"display window outside operation for {row['operation']}", (row["operation"],)))

        linked_takes: Counter[str] = Counter()
        for checkout in self.raw["checkout"]:
            _cents(checkout["net"], "checkout.net")
            _iso(checkout["date"], "checkout.date")
            take_id = checkout["take"]
            if take_id is None:
                continue
            take = self.takes.get(take_id)
            if take is None:
                self.issues.append(Issue("checkout_unknown_take", "error", "checkout references a missing take; revenue cannot be attributed", (checkout["id"], str(take_id))))
                continue
            if take["sku"] != checkout["sku"]:
                self.issues.append(Issue("checkout_sku_mismatch", "error", "checkout sku differs from its take", (checkout["id"], take_id)))
            if take["operation"] not in self.operations:
                self.issues.append(Issue("take_unknown_operation", "error", "take references a missing operation", (take_id,)))
            linked_takes[take_id] += 1

        for take_id, count in linked_takes.items():
            if count > 1:
                self.issues.append(Issue("duplicate_checkout_for_take", "error", "one take is linked to several checkouts", (take_id,)))

        for take in self.raw["take"]:
            _hours(take["hour"], "take.hour")
            _iso(take["date"], "take.date")
            operation = self.operation_for(take)
            if operation is None:
                continue
            studio = self.studio_for(take["operation"], take["sku"])
            if studio is None:
                self.issues.append(Issue("take_without_display", "error", "take sku is not unambiguously displayed at the operation", (take["id"],)))
                continue
            if not any(start <= take["hour"] < end for start, end in self.display_windows(take["operation"], studio)):
                self.issues.append(Issue("take_outside_display", "warning", "take falls outside the studio's display window", (take["id"],)))
            held = [(row["from_hour"], row["to_hour"]) for row in self.holds_for(operation["cabinet"], operation["date"]) if row["from_hour"] <= take["hour"] < row["to_hour"]]
            if held:
                self.issues.append(Issue("take_during_hold", "warning", "pickup recorded while the cabinet was held for a lecture or closure", (take["id"], operation["cabinet"])))

        for hold in self.raw["holds"]:
            _hours(hold["from_hour"], "holds.from_hour")
            _hours(hold["to_hour"], "holds.to_hour")
            _iso(hold["date"], "holds.date")
            _iso(hold["scheduled_on"], "holds.scheduled_on")
            if hold["to_hour"] < hold["from_hour"]:
                self.issues.append(Issue("inverted_hold", "error", "hold ends before it starts", (hold["cabinet"], hold["date"])))

        for row in self.raw["bridges"]:
            if row["old_cab"] not in {o["cabinet"] for o in self.raw["operation"]} and row["old_cab"] not in {n["cabinet"] for n in self.raw["next_layout"]}:
                self.issues.append(Issue("bridge_unknown_old", "warning", f"bridge old cabinet {row['old_cab']} has no operation", (row["old_cab"],)))
            if row["new_cab"] not in {n["cabinet"] for n in self.raw["next_layout"]}:
                self.issues.append(Issue("bridge_unknown_new", "warning", f"bridge new cabinet {row['new_cab']} has no next layout", (row["new_cab"],)))

        for row in self.raw["next_layout"]:
            _iso(row["date"], "next_layout.date")
            if row["gross_cm2"] - row["exclusion_cm2"] <= 0:
                self.issues.append(Issue("non_positive_next_capacity", "warning", f"next layout {row['cabinet']} has no positive area", (row["cabinet"],)))

        for row in self.raw["promise"]:
            _cents(row["min_cm2_hour"], "promise.min_cm2_hour")
            _iso(row["date"], "promise.date")
            _iso(row["confirmed_on"], "promise.confirmed_on")
            if row["min_cm2_hour"] < 0:
                self.issues.append(Issue("negative_promise", "error", "promised minimum is negative", (row["id"],)))

        for row in self.raw["candidate"]:
            _cents(row["cm2"], "candidate.cm2")
            _cents(row["fee"], "candidate.fee")
            _iso(row["date"], "candidate.date")
            if row["cm2"] < 0 or row["fee"] < 0:
                self.issues.append(Issue("negative_candidate", "error", "plan has negative area or fee", (row["plan"], row["cabinet"], row["studio"])))
            layout = self.next_layout_by_cabinet_date.get((row["cabinet"], row["date"]))
            if not layout:
                self.issues.append(Issue("candidate_without_layout", "error", f"plan {row['plan']} uses {row['cabinet']} with no next layout on {row['date']}", (row["plan"], row["cabinet"])))

    # -------------------------------------------------------------------- output
    def issue_dicts(self) -> list[dict[str, Any]]:
        return [issue.to_dict() for issue in self.issues]

    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)
