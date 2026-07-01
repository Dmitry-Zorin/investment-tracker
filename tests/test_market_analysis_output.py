import json
import unittest
from datetime import date, timedelta

from investment_tracker.market_analysis_calculations import build_instrument_analysis
from investment_tracker.market_analysis_output import (
    analysis_profile_notes,
    render_analysis_markdown,
    render_analytical_csv,
    render_market_chart,
    serializable_summary,
)


def rows(instrument_type="fund", count=90):
    result = []
    for index in range(count):
        row = {
            "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "board_id": "TQBR" if instrument_type == "fund" else "TQOB",
            "close": 100 + index / 10,
            "unit_value_rub": 100 + index / 10 if instrument_type == "fund" else 1030 + index,
            "accrued_interest": None if instrument_type == "fund" else 30 + index / 10,
            "volume": 100 + index,
            "value_rub": 10000 + index * 100,
        }
        if instrument_type == "bond":
            row["yield_close"] = 15 - index / 100
        result.append(row)
    return result


class MarketAnalysisOutputTests(unittest.TestCase):
    def setUp(self):
        fund = build_instrument_analysis(
            {"secid": "FUND", "type": "fund", "analysis_profile": "gold_fund"}, rows()
        )
        bond = build_instrument_analysis(
            {"secid": "BOND", "type": "bond", "analysis_profile": "government_bond"},
            rows("bond"),
        )
        self.model = {
            "schema_version": "1.0",
            "calculated_at": "2026-07-01",
            "latest_market_date": "2026-03-31",
            "instruments": [fund, bond],
            "warnings": [],
            "missing_data": [],
            "generated_from": [],
        }
        self.fund = fund
        self.bond = bond

    def test_profile_notes_are_type_specific_without_ticker_logic(self):
        gold = analysis_profile_notes("gold_fund")
        generic = analysis_profile_notes("generic_fund")

        self.assertIn("курс рубля", " ".join(gold["limitations"]))
        self.assertNotEqual(gold, generic)

    def test_summary_excludes_internal_rows(self):
        summary = serializable_summary(self.model)
        encoded = json.dumps(summary, ensure_ascii=False)

        self.assertNotIn("data_rows", encoded)
        self.assertNotIn('"rows"', encoded)
        self.assertIn("range_position_bucket", encoded)

    def test_markdown_has_stable_chatgpt_output_contract(self):
        markdown = render_analysis_markdown(self.model)

        self.assertIn("zone_context", markdown)
        self.assertIn("zone_interest", markdown)
        self.assertIn("confidence", markdown)
        self.assertIn("не является инвестиционной рекомендацией", markdown)

    def test_analytical_csv_fields_follow_instrument_type(self):
        fund_header = render_analytical_csv(self.fund).splitlines()[0]
        bond_header = render_analytical_csv(self.bond).splitlines()[0]

        self.assertTrue(fund_header.startswith("date,board_id,raw_close_rub,adjusted_close_rub"))
        self.assertIn("clean_price_percent", bond_header)
        self.assertIn("yield_close_percent", bond_header)

    def test_fund_chart_has_annotations_and_window(self):
        svg = render_market_chart(self.fund, "3m")

        self.assertIn('data-window="3m"', svg)
        self.assertIn("Минимум", svg)
        self.assertIn("Максимум", svg)
        self.assertIn("MA20", svg)
        self.assertIn('stroke-dasharray="6 4"', svg)
        self.assertEqual(svg.count('class="x-tick"'), 5)
        self.assertEqual(svg.count('class="y-tick"'), 5)
        self.assertIn('class="minimum-marker"', svg)
        self.assertIn('class="maximum-marker"', svg)
        self.assertIn('class="current-marker"', svg)
        self.assertIn('clip-path="url(#price-clip)"', svg)

    def test_bond_chart_has_separate_panels(self):
        svg = render_market_chart(self.bond, "1y")

        self.assertIn('id="price-panel"', svg)
        self.assertIn('id="yield-panel"', svg)
        self.assertIn("Чистая цена", svg)
        self.assertIn("Доходность к погашению", svg)


if __name__ == "__main__":
    unittest.main()
