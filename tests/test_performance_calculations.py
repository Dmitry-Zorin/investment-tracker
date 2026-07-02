import unittest
from datetime import date

from investment_tracker.performance_calculations import (
    benchmark_return,
    calculate_drawdown,
    calculate_period_return,
    calculate_position,
    calculate_ytd_return,
    calculate_volatility,
)


class PerformanceCalculationTests(unittest.TestCase):
    def test_fund_position_includes_fees(self):
        transactions = [
            {
                "instrument_id": "FUND1",
                "ticker": "FUND",
                "event_type": "buy",
                "event_date": "2026-06-01",
                "quantity": 100,
                "deal_amount": 1868,
                "paid_nkd": 0,
                "broker_fee": 2,
                "exchange_fee": 0.5,
                "tax": 0,
            }
        ]

        result = calculate_position(transactions, 18.9, date(2026, 7, 1))

        self.assertEqual(result.quantity, 100)
        self.assertAlmostEqual(result.cost_basis, 1870.5)
        self.assertAlmostEqual(result.market_value, 1890)
        self.assertAlmostEqual(result.total_pnl, 19.5)

    def test_buy_and_hold_return_uses_total_invested_equal_to_cost_basis(self):
        # No sells: total_invested must equal cost_basis, so simple_return is
        # unchanged versus the old open-lot-basis denominator (regression guard).
        transactions = [
            {
                "instrument_id": "FUND1",
                "ticker": "FUND",
                "event_type": "buy",
                "event_date": "2026-06-01",
                "quantity": 100,
                "deal_amount": 1868,
                "paid_nkd": 0,
                "broker_fee": 2,
                "exchange_fee": 0.5,
                "tax": 0,
            }
        ]

        result = calculate_position(transactions, 18.9, date(2026, 7, 1))

        self.assertAlmostEqual(result.total_invested, 1870.5)
        self.assertAlmostEqual(result.cost_basis, 1870.5)
        self.assertAlmostEqual(result.simple_return, 19.5 / 1870.5)

    def test_partial_sale_return_measured_against_total_invested(self):
        # Buy 10 @ basis 1000, later sell 5 (FIFO removes basis 500,
        # proceeds 600 -> realized 100), mark remaining 5 lots at 120.
        # total_invested stays 1000; open-lot cost_basis shrinks to 500.
        transactions = [
            {"instrument_id": "X", "ticker": "X", "event_type": "buy", "event_date": "2026-01-01", "quantity": 10, "deal_amount": 1000},
            {"instrument_id": "X", "ticker": "X", "event_type": "sell", "event_date": "2026-03-01", "quantity": 5, "deal_amount": 600},
        ]

        result = calculate_position(transactions, 120, date(2026, 7, 1))

        self.assertAlmostEqual(result.total_invested, 1000)
        self.assertAlmostEqual(result.cost_basis, 500)
        self.assertAlmostEqual(result.realized_pnl, 100)
        self.assertAlmostEqual(result.unrealized_pnl, 100)
        self.assertAlmostEqual(result.total_pnl, 200)
        self.assertAlmostEqual(result.simple_return, 200 / 1000)
        # Guard against the old bug that divided by the shrunken open-lot basis.
        self.assertNotAlmostEqual(result.simple_return, 200 / 500)

    def test_fully_closed_position_reports_real_return_not_none(self):
        # Buy 10 @ basis 1000, sell all 10 for 1300 -> realized 300.
        # No open lots: cost_basis == 0, but total_invested == 1000.
        transactions = [
            {"instrument_id": "X", "ticker": "X", "event_type": "buy", "event_date": "2026-01-01", "quantity": 10, "deal_amount": 1000},
            {"instrument_id": "X", "ticker": "X", "event_type": "sell", "event_date": "2026-03-01", "quantity": 10, "deal_amount": 1300},
        ]

        result = calculate_position(transactions, 130, date(2026, 7, 1))

        self.assertEqual(result.quantity, 0)
        self.assertAlmostEqual(result.cost_basis, 0)
        self.assertAlmostEqual(result.total_invested, 1000)
        self.assertAlmostEqual(result.total_pnl, 300)
        self.assertIsNotNone(result.simple_return)
        self.assertAlmostEqual(result.simple_return, 300 / 1000)

    def test_fifo_sale_removes_oldest_lot(self):
        transactions = [
            {"instrument_id": "X", "ticker": "X", "event_type": "buy", "event_date": "2026-01-01", "quantity": 10, "deal_amount": 100},
            {"instrument_id": "X", "ticker": "X", "event_type": "buy", "event_date": "2026-02-01", "quantity": 5, "deal_amount": 60},
            {"instrument_id": "X", "ticker": "X", "event_type": "sell", "event_date": "2026-03-01", "quantity": 10, "deal_amount": 110},
        ]

        result = calculate_position(transactions, 12, date(2026, 7, 1))

        self.assertEqual(result.quantity, 5)
        self.assertAlmostEqual(result.cost_basis, 60)
        self.assertAlmostEqual(result.realized_pnl, 10)
        self.assertAlmostEqual(result.total_pnl, 10)

    def test_coupon_and_tax_are_explicit_realized_result(self):
        transactions = [
            {"instrument_id": "B", "ticker": "B", "event_type": "buy", "event_date": "2026-01-01", "quantity": 1, "deal_amount": 900},
            {"instrument_id": "B", "ticker": "B", "event_type": "coupon", "event_date": "2026-04-01", "amount": 50},
            {"instrument_id": "B", "ticker": "B", "event_type": "tax", "event_date": "2026-04-01", "amount": 6.5},
        ]

        result = calculate_position(transactions, 900, date(2026, 7, 1))

        self.assertAlmostEqual(result.realized_pnl, 43.5)
        self.assertAlmostEqual(result.total_pnl, 43.5)

    def test_benchmark_uses_each_contribution_date(self):
        cash_flows = [(date(2026, 1, 1), 1000), (date(2026, 2, 1), 1000)]
        prices = [
            {"date": "2026-01-01", "unit_value_rub": 10},
            {"date": "2026-02-01", "unit_value_rub": 20},
            {"date": "2026-03-01", "unit_value_rub": 20},
        ]

        result = benchmark_return(cash_flows, prices, date(2026, 3, 1))

        self.assertAlmostEqual(result.invested, 2000)
        self.assertAlmostEqual(result.ending_value, 3000)
        self.assertAlmostEqual(result.return_value, 0.5)

    def test_drawdown_period_return_and_volatility(self):
        rows = [
            {"date": "2026-01-01", "unit_value_rub": 100},
            {"date": "2026-02-01", "unit_value_rub": 120},
            {"date": "2026-03-01", "unit_value_rub": 90},
        ]

        drawdown = calculate_drawdown([100, 120, 90])

        self.assertAlmostEqual(drawdown.current, -0.25)
        self.assertAlmostEqual(drawdown.maximum, -0.25)
        self.assertAlmostEqual(calculate_period_return(rows, 2), -0.1)
        self.assertIsNotNone(calculate_volatility(rows))

    def test_ytd_return_uses_first_quote_of_calendar_year(self):
        rows = [
            {"date": "2025-12-30", "unit_value_rub": 90},
            {"date": "2026-01-05", "unit_value_rub": 100},
            {"date": "2026-06-30", "unit_value_rub": 110},
        ]

        self.assertAlmostEqual(calculate_ytd_return(rows), 0.1)
        self.assertIsNone(calculate_ytd_return(rows[:1]))


if __name__ == "__main__":
    unittest.main()
