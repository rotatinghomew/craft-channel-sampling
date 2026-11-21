"""核查练习数据的原始事实，不计算分析结果。"""
from pathlib import Path
import json
import unittest


class DrillDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'data' / 'drill.json'
        cls.data = json.loads(source.read_text(encoding='utf-8'))

    def test_schema(self):
        expected = {'operation': ('id', 'cabinet', 'layout', 'date', 'from_hour', 'to_hour', 'gross_cm2', 'exclusion_cm2'), 'shares': ('operation', 'studio', 'from_hour', 'to_hour', 'confirmed_percent'), 'display': ('operation', 'sku', 'studio', 'from_hour', 'to_hour', 'visitors'), 'take': ('id', 'operation', 'sku', 'hour', 'date'), 'checkout': ('id', 'take', 'sku', 'cash_desk', 'date', 'net'), 'holds': ('cabinet', 'date', 'from_hour', 'to_hour', 'kind', 'scheduled_on'), 'bridges': ('old_cab', 'new_cab', 'supported'), 'promise': ('id', 'studio', 'cabinet', 'date', 'from_hour', 'to_hour', 'min_cm2_hour', 'confirmed_on'), 'candidate': ('plan', 'cabinet', 'studio', 'date', 'from_hour', 'to_hour', 'cm2', 'fee')}
        self.assertEqual(set(self.data), set(expected))
        for name, fields in expected.items():
            with self.subTest(table=name):
                self.assertTrue(self.data[name])
                self.assertTrue(all(set(row) == set(fields) for row in self.data[name]))

    def test_covering_events_and_sales_reconciliation_inputs(self):
        promises = self.data['promise']
        for row in promises[:2]:
            self.assertTrue(any(hold['cabinet'] == row['cabinet'] and hold['date'] == row['date'] and hold['scheduled_on'] > row['confirmed_on'] for hold in self.data['holds']))
        picks = {row['id']: row for row in self.data['take']}
        operations = {row['id']: row for row in self.data['operation']}
        self.assertTrue(any(row['take'] is None for row in self.data['checkout']))
        self.assertTrue(any(operations[picks[row['take']]['operation']]['cabinet'] != row['cash_desk'] for row in self.data['checkout'] if row['take']))
        self.assertGreater(sum(row['old_cab'] == 'S2' and not row['supported'] for row in self.data['bridges']), 1)
