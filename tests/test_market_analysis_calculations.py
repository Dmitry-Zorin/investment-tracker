import unittest
from datetime import date, timedelta

from investment_tracker.market_analysis_calculations import (
    build_instrument_analysis,
    classify_drawdown,
    classify_range_position,
    enrich_market_rows,
    select_window,
)


def fund_rows(count=90, start=date(2025, 1, 1), step=1):
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "board_id": "TQBR",
            "close": 100 + index * step,
            "unit_value_rub": 100 + index * step,
            "volume": 10 + index,
            "value_rub": 1000 + index * 100,
        }
        for index in range(count)
    ]


class MarketAnalysisCalculationTests(unittest.TestCase):
    def test_enrichment_uses_adjusted_price_and_warm_ma(self):
        instrument = {
            "secid": "FUND",
            "type": "fund",
            "corporate_actions": [
                {"type": "split", "effective_date": "2025-02-01", "ratio": 10}
            ],
        }

        enriched = enrich_market_rows(instrument, fund_rows())

        self.assertEqual(enriched[0]["adjustment_factor"], 10)
        self.assertIsNone(enriched[18]["ma20"])
        self.assertIsNotNone(enriched[19]["ma20"])
        self.assertIsNotNone(enriched[59]["ma60"])

    def test_three_month_window_uses_calendar_boundary(self):
        rows = fund_rows(220, date(2025, 12, 1))

        selected, requested_start, status = select_window(rows, "3m")

        self.assertEqual(requested_start, "2026-04-08")
        self.assertEqual(selected[0]["date"], "2026-04-08")
        self.assertEqual(status, "complete")

    def test_partial_window_keeps_available_history(self):
        rows = fund_rows(30)

        selected, _, status = select_window(rows, "5y")

        self.assertEqual(selected, rows)
        self.assertEqual(status, "partial")

    def test_window_metrics_include_turnover_and_warm_ma(self):
        result = build_instrument_analysis(
            {"secid": "FUND", "type": "fund", "analysis_profile": "generic_fund"},
            fund_rows(),
        )

        window = result["windows"]["3m"]
        self.assertIn("median_turnover_rub", window)
        self.assertIn("last_to_median_20d_turnover", window)
        self.assertIsNotNone(window["ma20"])
        self.assertIn(
            window["range_position_bucket"],
            {"near_low", "lower_range", "middle_range", "upper_range", "near_high"},
        )

    def test_bucket_boundaries_are_exact(self):
        self.assertEqual(classify_range_position(0.10), "near_low")
        self.assertEqual(classify_range_position(0.40), "middle_range")
        self.assertEqual(classify_range_position(0.90), "near_high")
        self.assertEqual(classify_drawdown(-0.01), "none_or_minimal")
        self.assertEqual(classify_drawdown(-0.05), "shallow")
        self.assertEqual(classify_drawdown(-0.10), "moderate")
        self.assertEqual(classify_drawdown(-0.1001), "deep")

    def test_bond_metrics_ignore_missing_yield(self):
        rows = [
            {
                "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
                "board_id": "TQOB",
                "close": 80 + index,
                "unit_value_rub": 830 + index,
                "accrued_interest": 30,
                "yield_close": None if index == 0 else 15 - index,
                "volume": 100,
                "value_rub": 10000,
            }
            for index in range(3)
        ]

        result = build_instrument_analysis(
            {"secid": "BOND", "type": "bond", "analysis_profile": "government_bond"},
            rows,
        )

        window = result["windows"]["all"]
        self.assertEqual(window["latest_yield_percent"], 13)
        self.assertEqual(window["minimum_yield_percent"], 13)
        self.assertEqual(window["maximum_yield_percent"], 14)

    def test_gap_over_fourteen_days_warns(self):
        rows = fund_rows(2)
        rows[1]["date"] = "2025-02-01"

        result = build_instrument_analysis(
            {"secid": "FUND", "type": "fund", "analysis_profile": "generic_fund"},
            rows,
        )

        self.assertTrue(any("31" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
