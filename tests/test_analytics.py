"""Analytic invariants: capacity, attribution, reconciliation, plans and trace.

These tests encode the business rules, not the particular synthetic numbers,
except where a specific figure is the point being protected.
"""
from pathlib import Path
import json
import sys
import unittest
from fractions import Fraction

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cabinet_analytics import (  # noqa: E402
    DrillData,
    ambiguity_report,
    build_ledger,
    cabinet_capacity_summary,
    decompose_layouts,
    evaluate_plan,
    next_capacity,
    notification_impact,
    pending_value_range,
    plan_sensitivity,
    plan_summary,
    position_performance,
    report,
    resolve_bridges,
    share_positions,
    studio_capacity_summary,
    studio_yield_table,
    trace_promise,
    trace_take,
)


def load_drill():
    return DrillData.load(ROOT / "data" / "drill.json")


class CapacityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_drill()

    def test_effective_area_deducts_exclusions(self):
        summary = cabinet_capacity_summary(self.data)
        for entry in summary:
            self.assertEqual(
                entry["effective_cm2"],
                next(
                    row["gross_cm2"] - row["exclusion_cm2"]
                    for row in self.data.raw["operation"]
                    if row["cabinet"] == entry["cabinet"]
                ),
            )

    def test_hours_are_half_open_and_held_hours_are_excluded(self):
        operation = self.data.operations["3-S1"]
        summary = next(
            entry for entry in cabinet_capacity_summary(self.data, cabinet="S1")
            if entry["layout"] == "V1"
        )
        self.assertEqual(summary["open_hours"], 14 * 8)
        self.assertEqual(summary["held_hours"], 3)
        self.assertEqual(summary["available_hours"], summary["open_hours"] - summary["held_hours"])
        self.assertEqual(summary["area_hours"], 8000 * (14 * 8 - 3))
        # The lecture on 08-04 13:00-16:00 is inside 09:00-17:00.
        self.assertEqual(operation["date"], "2026-08-04")

    def test_held_hours_are_not_sellable_opportunity(self):
        capacity = next(
            entry for entry in cabinet_capacity_summary(self.data, cabinet="S1")
            if entry["layout"] == "V1"
        )
        self.assertEqual(capacity["held_hours"], 3)
        # Held hours never appear as "free later": the studio positions on the
        # operation with a lecture only carry the non-held hours.
        for position in share_positions(self.data, self.data.operations["3-S1"]):
            self.assertEqual(position.hours, 5)
        unheld = share_positions(self.data, self.data.operations["0-S1"])
        self.assertTrue(all(position.hours == 8 for position in unheld))

    def test_unallocated_display_hours_are_reported_not_silently_sold(self):
        capacity = next(e for e in cabinet_capacity_summary(self.data, cabinet="S2") if e["layout"] == "V1")
        self.assertEqual(capacity["display_covered_hours"], 104)
        self.assertEqual(capacity["unallocated_hours"], 8)

    def test_share_percentages_are_used_as_signed(self):
        positions = share_positions(self.data, self.data.operations["0-S1"])
        by_studio = {position.studio: position for position in positions}
        self.assertEqual(by_studio["甲"].area_hours_point, 8000 * 0.6 * 8)
        self.assertEqual(by_studio["乙"].area_hours_point, 8000 * 0.4 * 8)

    def test_unconfirmed_share_is_a_range_and_is_not_split(self):
        positions = share_positions(self.data, self.data.operations["14-S1"])
        by_studio = {position.studio: position for position in positions}
        # 甲 signed 60%, 乙 unconfirmed.  The 40% residual is 乙's upper bound only.
        self.assertEqual(by_studio["甲"].area_hours_point, 8000 * 0.6 * 8)
        self.assertEqual(by_studio["乙"].area_hours_point, None)
        self.assertEqual(by_studio["乙"].area_hours_low, 0)
        self.assertEqual(by_studio["乙"].area_hours_high, 8000 * 0.4 * 8)
        self.assertEqual(by_studio["乙"].residual_percent, Fraction(2, 5))
        # A point total must not silently sum a range.
        totals = {
            entry["cabinet"]: entry for entry in studio_capacity_summary(self.data) if entry["studio"] == "乙"
        }
        self.assertIsNone(totals["S1"]["area_hours_point"])
        self.assertLess(totals["S1"]["area_hours_low"], totals["S1"]["area_hours_high"])

    def test_next_capacity_excludes_closure(self):
        capacity = next_capacity(self.data, "S2", "2026-09-02")
        self.assertEqual(capacity["effective_cm2"], 7000)
        self.assertEqual(capacity["open_hours"], 8)
        self.assertEqual(capacity["held_hours"], 8)
        self.assertEqual(capacity["area_hours"], 0)


class RevenueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_drill()
        cls.ledger = build_ledger(cls.data)

    def test_cashier_reconciliation(self):
        self.assertTrue(self.ledger.reconciles)
        self.assertEqual(
            self.ledger.attributed_cents + self.ledger.unattributed_cents,
            self.ledger.cashier_total_cents,
        )
        self.assertEqual(self.ledger.cashier_total_cents, sum(row["net"] for row in self.data.raw["checkout"]))
        self.assertGreater(self.ledger.unattributed_cents, 0)
        self.assertEqual(self.ledger.unattributed_cents, sum(row["net"] for row in self.ledger.unattributed_rows))

    def test_cross_desk_and_delayed_checkout_stay_with_pickup_cabinet(self):
        for row in self.ledger.by_take.values():
            take = self.data.takes[row["take"]]
            operation = self.data.operations[take["operation"]]
            self.assertEqual(row["cabinet"], operation["cabinet"])
            self.assertEqual(row["layout"], operation["layout"])
            self.assertEqual(row["take_date"], take["date"])
        self.assertGreater(len(self.ledger.delayed_rows), 0)
        self.assertGreater(len(self.ledger.cross_desk_rows), 0)

    def test_unattributed_never_promoted_to_attribution(self):
        self.assertEqual(self.ledger.diagnostics["note"].split(";")[0], "same sku/day counts are not enough to pair a checkout with a pickup")
        self.assertTrue(all(row["status"] == "unattributed" for row in self.ledger.unattributed_rows))
        self.assertTrue(all(row["status"] == "identified_unsettled" for row in self.ledger.pending_take_rows))
        self.assertEqual(len(self.ledger.pending_take_rows), self.ledger.pending_takes)

    def test_pending_value_is_a_band_not_revenue(self):
        band = pending_value_range(self.data)
        self.assertLessEqual(band["low_cents"], band["high_cents"])
        self.assertNotEqual(band["low_cents"], 0)
        self.assertIn("valuation band", band["basis"])
        self.assertEqual(band["takes"], self.ledger.pending_takes)

    def test_visitors_are_not_summed_as_independent_footfall(self):
        for performance in position_performance(self.data):
            # A ratio is only offered per product observation.
            if performance.visitors:
                self.assertIsNotNone(performance.to_dict()["pickups_per_observed_visitor"])
        yields = studio_yield_table(self.data)
        for entry in yields:
            self.assertNotIn("visitors", entry)
            self.assertEqual(
                sum(observation["visitors"] for observation in entry["visitor_observations"]),
                sum(
                    p.visitors
                    for p in position_performance(self.data)
                    if p.studio == entry["studio"] and p.cabinet == entry["cabinet"] and p.layout == entry["layout"]
                ),
            )

    def test_yield_denominator_uses_signed_share_not_whole_cabinet(self):
        yields = {
            (row["studio"], row["cabinet"], row["layout"]): row for row in studio_yield_table(self.data)
        }
        entry = yields[("甲", "S1", "V1")]
        self.assertAlmostEqual(
            entry["yield_low_cents_per_area_hour"],
            entry["revenue_cents"] / entry["confirmed_area_hours"],
        )
        # The denominator is 甲's own signed 60% block, not the whole cabinet.
        cabinet = next(e for e in cabinet_capacity_summary(self.data, cabinet="S1") if e["layout"] == "V1")
        self.assertAlmostEqual(
            entry["confirmed_area_hours"],
            cabinet["area_hours"] * 0.6,
        )

    def test_unconfirmed_studio_cannot_look_efficient_by_a_small_denominator(self):
        yields = {
            (row["studio"], row["cabinet"], row["layout"]): row for row in studio_yield_table(self.data)
        }
        unconfirmed = yields[("乙", "S1", "V2")]
        self.assertEqual(unconfirmed["confirmed_area_hours"], 0)
        self.assertGreater(unconfirmed["residual_area_hours"], 0)
        # The conservative band uses the full residual as denominator.
        self.assertAlmostEqual(
            unconfirmed["yield_low_cents_per_area_hour"],
            unconfirmed["revenue_cents"] / unconfirmed["residual_area_hours"],
        )


class LayoutChangeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_drill()

    def test_ununique_bridge_is_not_merged(self):
        bridges = resolve_bridges(self.data)
        self.assertFalse(bridges.complete)
        self.assertEqual(bridges.ambiguous_old["S2"], ["S2", "S3"])
        self.assertNotIn("S2", bridges.unique)

    def test_ambiguous_cabinet_is_excluded_from_decomposition(self):
        decomposition = decompose_layouts(self.data)
        self.assertTrue(all(row.cabinet != "S2" for row in decomposition))
        report_rows = ambiguity_report(self.data)
        self.assertIn("S2", report_rows["affected_old_cabinets"])

    def test_effects_sum_to_change(self):
        for row in decompose_layouts(self.data):
            payload = row.to_dict()
            self.assertEqual(sum(payload["effects_cents"].values()), payload["change_cents"])
            self.assertEqual(payload["residual_cents"], 0)
            # Every factor is named so a change can be attributed to a cause.
            self.assertEqual(
                set(payload["effects_cents"]),
                {"opportunity", "traffic", "conversion", "settlement", "value"},
            )


class PlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_drill()
        cls.summary = plan_summary(cls.data)
        cls.trials = {trial["plan"]: trial for trial in cls.summary["plans"]}

    def test_every_plan_is_evaluated(self):
        self.assertEqual(set(self.trials), {"P0", "P1", "P2"})

    def test_over_capacity_plan_is_not_summed_as_improvement(self):
        for plan in ("P1", "P2"):
            self.assertTrue(self.trials[plan]["overlapping_improvements"])
            self.assertFalse(self.trials[plan]["feasible"])
            over = [entry for entry in self.trials[plan]["capacity"].values() if entry["over_capacity"]]
            self.assertTrue(over)

    def test_closed_cabinet_cannot_deliver_a_signed_minimum(self):
        trial = evaluate_plan(self.data, "P0")
        check = next(c for c in trial.promise_checks if c.promise == "A2")
        self.assertEqual(check.delivered_area_hours, 0)
        self.assertEqual(check.gap_area_hours, 40000)
        self.assertEqual(check.status, "short")
        self.assertEqual(trial.conservative_fee_cents, 4200)
        self.assertEqual(trial.total_fee_cents, 11600)

    def test_shared_priority_slot_is_a_conflict_not_a_gain(self):
        trial = evaluate_plan(self.data, "P2")
        conflicts = trial.allocation_conflicts
        self.assertTrue(any(conflict["code"] == "priority_slot_promised_to_multiple_studios" for conflict in conflicts))
        # A reconstruction to S3 without a new signature is not a met promise.
        reassigned = [c for c in trial.promise_checks if c.status == "reassignment_required"]
        self.assertTrue(reassigned)

    def test_new_notice_does_not_lower_the_committed_minimum(self):
        impact = notification_impact(self.data)
        affected = {row["promise"]: row for row in impact["affected_commitments"]}
        self.assertIn("A1", affected)
        self.assertIn("A2", affected)
        self.assertEqual(affected["A1"]["min_cm2_hour_unchanged"], 29000)
        self.assertEqual(affected["A2"]["min_cm2_hour_unchanged"], 40000)
        self.assertEqual(affected["A2"]["available_hours_after_notice"], 0)
        # Adjustable opportunity never includes held hours.
        for windows in impact["adjustable_remaining_opportunity"].values():
            for window in windows:
                self.assertGreater(window["hours"], 0)

    def test_fees_and_projected_sales_are_separate(self):
        for trial in self.trials.values():
            self.assertIn("projected_sales_low_cents", trial)
            self.assertNotIn("total", trial)
            self.assertEqual(
                trial["committed_fee_cents"] + trial["new_fee_cents"],
                trial["conservative_fee_cents"],
            )
            self.assertEqual(
                trial["conservative_fee_cents"] + trial["at_risk_fee_cents"],
                trial["total_fee_cents"],
            )

    def test_sensitivity_does_not_move_money(self):
        sensitivity = plan_sensitivity(self.data)
        self.assertIn("linked_only", sensitivity["rankings"])
        self.assertTrue(all(ranking and all(plan in sensitivity["sales_by_view"]["linked_only"] for plan in ranking) for ranking in sensitivity["rankings"].values()))
        self.assertEqual(
            len(sensitivity["sales_by_view"]["linked_only"]),
            len(self.trials),
        )
        self.assertIn("not evidence", sensitivity["note"])


class TraceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_drill()

    def test_take_trace_reaches_layout_and_checkout(self):
        trace = trace_take(self.data, "P3-S1-0-2")
        self.assertEqual(trace.rows["operation"]["layout"], "V1")
        self.assertEqual(trace.rows["checkout"]["cash_desk"], "S2")
        self.assertEqual(trace.rows["checkout"]["take_date"], "2026-08-04")
        self.assertIn("held", " ".join(trace.findings))

    def test_take_trace_reports_unsettled_pickup(self):
        trace = trace_take(self.data, "P5-S1-0-0")
        self.assertIsNone(trace.rows["checkout"])
        self.assertTrue(any("identified but unsettled" in finding for finding in trace.findings))

    def test_promise_trace_shows_signed_and_actual(self):
        trace = trace_promise(self.data, "P0", "A2")
        self.assertEqual(trace.rows["promise"]["min_cm2_hour"], 40000)
        self.assertEqual(trace.rows["check"]["delivered_area_hours"], 0)
        self.assertEqual(trace.rows["holds_on_cabinet"][0]["kind"], "闭馆")


class SyntheticEdgeCaseTest(unittest.TestCase):
    """Edge cases built from the real table shape, not from the drill numbers."""

    def base(self):
        return json.loads((ROOT / "data" / "drill.json").read_text(encoding="utf-8"))

    def test_duplicate_share_over_100_is_an_error(self):
        raw = self.base()
        raw["shares"].append({"operation": "0-S1", "studio": "丙", "from_hour": 9, "to_hour": 17, "confirmed_percent": 20})
        data = DrillData.from_raw(raw)
        self.assertTrue(any(issue.code == "share_percent_over_100" for issue in data.issues))
        self.assertTrue(data.has_errors())

    def test_duplicate_checkout_for_take_is_an_error(self):
        raw = self.base()
        row = next(r for r in raw["checkout"] if r["take"])
        raw["checkout"].append({**row, "id": "O-dup", "net": 1})
        data = DrillData.from_raw(raw)
        self.assertTrue(any(issue.code == "duplicate_checkout_for_take" for issue in data.issues))

    def test_checkout_sku_mismatch_is_an_error(self):
        raw = self.base()
        row = next(r for r in raw["checkout"] if r["take"])
        row["sku"] = "K9"
        data = DrillData.from_raw(raw)
        self.assertTrue(any(issue.code == "checkout_sku_mismatch" for issue in data.issues))
        # Neither the mismatched net nor sku is silently attributed elsewhere.
        ledger = build_ledger(data)
        self.assertTrue(ledger.reconciles)
        self.assertEqual(ledger.attributed_cents + ledger.unattributed_cents, ledger.cashier_total_cents)

    def test_unknown_take_checkout_is_unattributed_but_reconciled(self):
        raw = self.base()
        row = next(r for r in raw["checkout"] if r["take"])
        row["take"] = "missing-take"
        data = DrillData.from_raw(raw)
        ledger = build_ledger(data)
        self.assertTrue(any(row.get("reason") for row in ledger.unattributed_rows))
        self.assertEqual(ledger.attributed_cents + ledger.unattributed_cents, ledger.cashier_total_cents)
        self.assertTrue(ledger.reconciles)

    def test_display_split_across_two_skus_does_not_double_the_share(self):
        raw = self.base()
        # Give studio 甲 a second product on operation 0-S1 for the same hours.
        raw["display"].append({"operation": "0-S1", "sku": "K9", "studio": "甲", "from_hour": 9, "to_hour": 15, "visitors": 5})
        data = DrillData.from_raw(raw)
        yields = {
            (row["studio"], row["cabinet"], row["layout"]): row for row in studio_yield_table(data)
        }
        single = studio_yield_table(load_drill())
        single_map = {(row["studio"], row["cabinet"], row["layout"]): row for row in single}
        self.assertAlmostEqual(
            yields[("甲", "S1", "V1")]["signed_area_hours"],
            single_map[("甲", "S1", "V1")]["signed_area_hours"],
        )

    def test_no_share_row_still_shows_opportunity_without_inventing_a_share(self):
        raw = self.base()
        raw["shares"] = [row for row in raw["shares"] if not (row["operation"] == "0-S1" and row["studio"] == "甲")]
        data = DrillData.from_raw(raw)
        self.assertTrue(any(issue.code == "display_without_share" for issue in data.issues))
        performance = [
            p for p in position_performance(data) if p.studio == "甲" and p.cabinet == "S1" and p.layout == "V1"
        ]
        self.assertTrue(performance)
        for position in performance:
            self.assertGreater(position.signed_area_hours, 0)


class ReportTest(unittest.TestCase):
    def test_report_is_json_serialisable_and_reconciles(self):
        payload = report(load_drill())
        text = json.dumps(payload, ensure_ascii=False)
        reparsed = json.loads(text)
        self.assertTrue(reparsed["unattributed"]["reconciles"])
        self.assertIn("definitions", reparsed)
        self.assertEqual(
            reparsed["revenue"]["attributed_cents"] + reparsed["revenue"]["unattributed_cents"],
            reparsed["revenue"]["cashier_total_cents"],
        )
        self.assertEqual(len(reparsed["plans"]["plans"]), 3)
        self.assertIn("trace_hint", reparsed)


if __name__ == "__main__":
    unittest.main()
