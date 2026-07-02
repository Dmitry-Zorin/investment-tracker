import csv
import hashlib
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from investment_tracker.performance_calculations import calculate_position
from investment_tracker.performance_output import (
    _transactions_for_position,
    aggregate_ticker_views,
    build_market_summary,
    export_chatgpt,
    render_bar_chart,
    render_line_chart,
    render_multi_line_chart,
    render_period_returns_chart,
    render_performance_report,
    validate_portfolio_outputs,
    write_outputs,
)


class PerformanceOutputTests(unittest.TestCase):
    def setUp(self):
        self.model = {
            "calculated_at": "2026-07-01",
            "brokerage_snapshot_date": "2026-06-29",
            "latest_market_date": "2026-06-30",
            "benchmarks": ["CASH"],
            "data_status": {
                "brokerage_snapshot": "confirmed",
                "market_prices": "estimated_from_moex",
                "commissions": "confirmed",
                "accrued_coupon_income": "confirmed",
                "taxes": "not_calculated",
                "bank_deposit_benchmark": "not_available",
                "intraday_spreads": "not_included",
                "entry_exit_timing": "out_of_scope",
            },
            "portfolio": {
                "total_cost_basis_rub": 1800,
                "confirmed_value_rub": 1850,
                "estimated_market_value_rub": 1860,
                "confirmed_pnl_rub": 50,
                "confirmed_pnl_pct": 0.0277,
                "estimated_market_pnl_rub": 60,
                "estimated_market_pnl_pct": 0.0333,
                "cash_rub": 25,
                "positions_count": 1,
            },
            "positions": [
                {
                    "ticker": "CASH",
                    "name": "Mock Cash Fund",
                    "account_id": "IIS",
                    "account_alias": "IIS",
                    "quantity": 100,
                    "avg_entry_price_rub": 18,
                    "cost_basis": 1800,
                    "confirmed_value": 1850,
                    "confirmed_value_components": {
                        "clean_value": 1850,
                        "accrued_interest": 0,
                        "total_value": 1850,
                    },
                    "asset_type": "fund_money_market",
                    "asset_type_label": "Фонд денежного рынка",
                    "source_asset_type": "money_market_fund",
                    "confirmed_pnl": 50,
                    "confirmed_return": 0.0277,
                    "preliminary_value": 1860,
                    "preliminary_pnl": 60,
                    "preliminary_return": 0.0333,
                    "public_return_since_entry": 0.03,
                    "benchmark_return": 0.03,
                    "current_drawdown": 0,
                    "max_drawdown": -0.001,
                    "volatility": None,
                    "holding_days": 20,
                    "annualized_return": None,
                    "last_entry_date": "2026-06-10",
                    "commissions_rub": 2,
                    "accrued_coupon_income_rub": None,
                    "period_returns": {"1m": None, "3m": None, "6m": None, "12m": None, "ytd": 0.02},
                    "market_date": "2026-06-30",
                    "source": "brokerage_snapshot",
                }
            ],
            "pnl_contribution": [
                {
                    "ticker": "CASH",
                    "pnl_rub": 50,
                    "pnl_pct": 0.0277,
                    "share_of_total_pnl_pct": 1.0,
                    "portfolio_impact_pct": 50 / 1875,
                }
            ],
            "benchmark_comparison": [
                {
                    "ticker": "CASH",
                    "benchmark_ticker": "CASH",
                    "method": "lot_based",
                    "entry_date": "2026-06-10",
                    "instrument_return_since_entry_pct": 0.0333,
                    "benchmark_return_same_period_pct": 0.03,
                    "difference_pct_points": 0.0033,
                    "result_vs_benchmark_rub": 6,
                    "interpretation": "approximately_equal",
                    "limitations": [],
                }
            ],
            "instrument_period_returns": [
                {
                    "ticker": "CASH",
                    "name": "Mock Cash Fund",
                    "asset_class": "fund_money_market",
                    "returns_pct": {"1m": None, "3m": None, "6m": None, "12m": None, "ytd": 0.02},
                    "last_quote_date": "2026-06-30",
                    "source": "MOEX",
                }
            ],
            "warnings": [
                {
                    "severity": "info",
                    "code": "short_holding_period",
                    "message": "Annualization omitted for holdings shorter than 30 days.",
                    "affected_items": ["CASH"],
                }
            ],
            "missing_data": ["No complete one-month holding period."],
            "generated_from": [
                {"path": "brokerage-current.json", "sha256": "a" * 64, "role": "brokerage_snapshot"}
            ],
        }

    def test_report_has_required_portfolio_sections_and_scope(self):
        report = render_performance_report(self.model)

        self.assertIn("# Portfolio Performance Report", report)
        for heading in (
            "## 1. Metadata",
            "## 2. Purpose and scope",
            "## 3. Executive summary",
            "## 4. Data status",
            "## 5. Portfolio summary",
            "## 6. Positions",
            "## 7. PnL contribution",
            "## 8. Comparison vs configured benchmark",
            "## 9. Instrument period returns",
            "## 10. Charts",
            "## 11. Data limitations",
            "## 12. Not included in this package",
        ):
            self.assertIn(heading, report)
        self.assertIn("Purpose: portfolio performance control and instrument comparison.", report)
        self.assertIn("Timing and market-zone analysis is handled by a separate market-analysis.zip package.", report)
        self.assertIn("Confirmed by brokerage snapshot", report)
        self.assertIn("Estimated from MOEX/public market data", report)
        self.assertIn("Largest negative contribution: n/a", report)
        self.assertNotIn("recommendation: buy", report.lower())
        self.assertFalse(any(line.endswith(" ") for line in report.splitlines()))

    def test_svg_contains_full_history_and_dates(self):
        rows = [
            {"date": "2026-06-01", "unit_value_rub": 10},
            {"date": "2026-06-30", "unit_value_rub": 11},
        ]

        svg = render_line_chart("CASH", rows, "unit_value_rub", unit_label="RUB per unit")

        self.assertIn("<svg", svg)
        self.assertIn("2026-06-01", svg)
        self.assertIn("2026-06-30", svg)
        self.assertIn("Source: MOEX ISS", svg)
        self.assertIn("Values: RUB per unit", svg)

    def test_summary_uses_null_for_missing_values(self):
        summary = build_market_summary(self.model)

        self.assertIsNone(summary["positions"][0]["accrued_coupon_income_rub"])
        self.assertEqual(summary["metadata"]["last_market_quote_date"], "2026-06-30")
        self.assertEqual(summary["schema_version"], "1.0")
        self.assertEqual(summary["metadata"]["purpose"], "portfolio_performance_and_benchmark_comparison")
        self.assertIn("entry_exit_timing", summary["metadata"]["not_for"])
        self.assertEqual(summary["data_status"]["market_prices"], "estimated_from_moex")
        self.assertEqual(summary["portfolio"]["cash_rub"], 25)
        self.assertEqual(summary["pnl_contribution"][0]["ticker"], "CASH")
        self.assertEqual(summary["benchmark_comparison"][0]["method"], "lot_based")
        self.assertEqual(summary["instrument_period_returns"][0]["returns_pct"]["ytd"], 0.02)
        self.assertEqual(summary["positions"][0]["confirmed_value_rub"], 1850)
        self.assertEqual(summary["positions"][0]["asset_class"], "fund_money_market")

    def test_write_outputs_creates_required_csv_and_svg_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            write_outputs(root, self.model)

            data_files = {
                "portfolio-summary.csv",
                "positions.csv",
                "benchmark-comparison.csv",
                "instrument-period-returns.csv",
                "pnl-contribution.csv",
            }
            chart_files = {
                "portfolio-composition.svg",
                "positions-pnl.svg",
                "positions-vs-benchmark.svg",
                "instruments-period-returns.svg",
                "pnl-contribution.svg",
            }
            package = root / "reports/chatgpt-export"
            self.assertEqual({path.name for path in (package / "data").iterdir()}, data_files)
            self.assertEqual({path.name for path in (package / "charts").iterdir()}, chart_files)
            self.assertTrue((package / "performance.md").exists())
            self.assertTrue((package / "market-summary.json").exists())
            self.assertFalse((root / "reports/data").exists())
            self.assertFalse((root / "reports/charts").exists())
            with (package / "data/positions.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["ticker"], "CASH")
            self.assertEqual(rows[0]["accrued_coupon_income_rub"], "")

    def test_report_escapes_pipe_and_newline_in_position_name(self):
        # brokerage-supplied name is free-form; a pipe or newline must not be
        # able to corrupt the Markdown positions table.
        model = dict(self.model)
        position = dict(self.model["positions"][0])
        position["name"] = "Evil | Fund\nsecond line"
        model["positions"] = [position]

        report = render_performance_report(model)

        self.assertIn(r"Evil \| Fund second line", report)
        self.assertNotIn("Evil | Fund", report)

    def test_report_and_summary_disclose_split_adjustment(self):
        model = dict(self.model)
        model["corporate_actions"] = [
            {
                "secid": "BOND",
                "type": "split",
                "effective_date": "2021-06-04",
                "ratio": 100,
                "source": "https://www.moex.com/n34360",
            }
        ]

        report = render_performance_report(model)
        summary = build_market_summary(model)

        self.assertIn("BOND: цены до 2021-06-04 скорректированы", report)
        self.assertEqual(summary["corporate_actions"], model["corporate_actions"])

    def test_report_uses_warning_message_from_workspace(self):
        model = dict(self.model)
        model["warnings"] = [
            {
                "severity": "warning",
                "code": "workspace_warning",
                "message": "Workspace-provided warning.",
                "affected_items": [],
            }
        ]

        self.assertIn("Workspace-provided warning.", render_performance_report(model))

    def test_transactions_for_position_separates_same_instrument_by_account(self):
        ledger = [
            {"record_type": "transaction", "instrument_id": "INST-1", "account_id": "A", "event_id": "a"},
            {"record_type": "transaction", "instrument_id": "INST-1", "account_id": "B", "event_id": "b"},
            {"record_type": "transaction", "instrument_id": "OTHER", "account_id": "A", "event_id": "c"},
        ]

        result = _transactions_for_position(ledger, "INST-1", "A", "CASH")

        self.assertEqual([item["event_id"] for item in result], ["a"])
        self.assertEqual(result[0]["ticker"], "CASH")

    def test_ticker_views_aggregate_positions_across_accounts(self):
        positions = [
            {"ticker": "CASH", "cost_basis": 100, "confirmed_pnl": 10},
            {"ticker": "CASH", "cost_basis": 200, "confirmed_pnl": -5},
        ]
        comparisons = [
            {
                "ticker": "CASH", "benchmark_ticker": "CASH", "entry_date": "2026-06-10", "method": "lot_based",
                "instrument_invested_rub": 100, "instrument_ending_value_rub": 110,
                "benchmark_invested_rub": 100, "benchmark_ending_value_rub": 108,
                "limitations": [],
            },
            {
                "ticker": "CASH", "benchmark_ticker": "CASH", "entry_date": "2026-06-20", "method": "lot_based",
                "instrument_invested_rub": 200, "instrument_ending_value_rub": 195,
                "benchmark_invested_rub": 200, "benchmark_ending_value_rub": 202,
                "limitations": [],
            },
        ]

        pnl, benchmark = aggregate_ticker_views(
            positions, comparisons, total_pnl=5, total_portfolio_value=1000
        )

        self.assertEqual(len(pnl), 1)
        self.assertEqual(pnl[0]["pnl_rub"], 5)
        self.assertAlmostEqual(pnl[0]["pnl_pct"], 5 / 300)
        self.assertEqual(len(benchmark), 1)
        self.assertEqual(benchmark[0]["entry_date"], "2026-06-10")
        self.assertAlmostEqual(benchmark[0]["result_vs_benchmark_rub"], -5)

    def test_aggregate_instrument_return_matches_position_simple_return_after_partial_sale(self):
        # A partially-sold position: buy 10 @ basis 1000, sell 5 (realized 100),
        # remaining 5 lots marked at 120 (unrealized 100) -> total_pnl 200,
        # total_invested 1000, simple_return 0.20. The per-position benchmark row
        # must roll up so the aggregate instrument return equals simple_return.
        transactions = [
            {"instrument_id": "X", "ticker": "X", "event_type": "buy", "event_date": "2026-01-01", "quantity": 10, "deal_amount": 1000},
            {"instrument_id": "X", "ticker": "X", "event_type": "sell", "event_date": "2026-03-01", "quantity": 5, "deal_amount": 600},
        ]
        result = calculate_position(transactions, 120, date(2026, 7, 1))
        self.assertAlmostEqual(result.simple_return, 0.20)

        # Build the benchmark row with the same corrected fields build_report_model uses.
        comparisons = [
            {
                "ticker": "X",
                "benchmark_ticker": "BENCH",
                "entry_date": "2026-01-01",
                "method": "lot_based",
                "instrument_invested_rub": result.total_invested,
                "instrument_ending_value_rub": result.total_invested + result.total_pnl,
                "benchmark_invested_rub": 1000,
                "benchmark_ending_value_rub": 1150,
                "limitations": [],
            }
        ]
        positions = [{"ticker": "X", "cost_basis": result.cost_basis, "confirmed_pnl": result.total_pnl}]

        _pnl, benchmark = aggregate_ticker_views(
            positions, comparisons, total_pnl=result.total_pnl, total_portfolio_value=10000
        )

        self.assertEqual(len(benchmark), 1)
        # Aggregate instrument return must exactly equal the per-position simple_return.
        self.assertAlmostEqual(benchmark[0]["instrument_return_since_entry_pct"], result.simple_return)
        # Money-weighted terminal value = invested + profit + returned principal.
        self.assertAlmostEqual(benchmark[0]["result_vs_benchmark_rub"], 1200 - 1150)

    def test_multi_line_chart_contains_each_named_series(self):
        series = {
            "CASH": [{"date": "2026-06-01", "normalized": 100}, {"date": "2026-06-30", "normalized": 101}],
            "BOND": [{"date": "2026-06-01", "normalized": 100}, {"date": "2026-06-30", "normalized": 98}],
        }

        svg = render_multi_line_chart("Сравнение", series, "normalized")

        self.assertIn(">CASH<", svg)
        self.assertIn(">BOND<", svg)
        self.assertEqual(svg.count("<polyline"), 2)

    def test_bar_chart_labels_every_category(self):
        svg = render_bar_chart(
            "Доходность",
            [("BOND", -0.8), ("GOV_BOND", -3.2), ("CASH", 0.8)],
            unit="percent",
        )

        self.assertNotIn("<polyline", svg)
        self.assertIn(">BOND<", svg)
        self.assertIn(">GOV_BOND<", svg)
        self.assertIn(">CASH<", svg)
        self.assertEqual(svg.count("<rect class=\"bar\""), 3)

    def test_bar_chart_rotates_long_labels_and_groups_values(self):
        # Composed labels for several positions would overlap horizontally, so
        # they must be rotated, and on-bar values use the same space-grouped
        # thousands as the report tables.
        categories = [(f"TICK{index} (1 234 567.89 RUB)", 1234567.89) for index in range(6)]

        svg = render_bar_chart("Positions vs benchmark", categories, unit="RUB")

        self.assertIn("rotate(-35", svg)
        self.assertIn(">1 234 567.89<", svg)

    def test_period_returns_chart_uses_readable_ticker_period_grid(self):
        svg = render_period_returns_chart(
            [
                {
                    "ticker": "CASH",
                    "returns_pct": {"1m": 0.01, "3m": 0.03, "6m": None, "12m": 0.1, "ytd": 0.05},
                }
            ]
        )

        self.assertIn(">CASH<", svg)
        self.assertIn(">1m<", svg)
        self.assertIn(">YTD<", svg)
        self.assertIn(">n/a<", svg)
        self.assertIn("not a timing signal", svg)

    def test_export_contains_exact_portfolio_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_outputs(root, self.model)

            export_chatgpt(root)

            export = root / "reports/chatgpt-export"
            self.assertTrue(export.exists())
            archive = root / "reports/chatgpt-export.zip"
            self.assertTrue(archive.exists())
            with zipfile.ZipFile(archive) as zipped:
                names = {name for name in zipped.namelist() if not name.endswith("/")}
            expected = {
                "chatgpt-export/performance.md",
                "chatgpt-export/market-summary.json",
                *(f"chatgpt-export/data/{name}" for name in (
                    "portfolio-summary.csv",
                    "positions.csv",
                    "benchmark-comparison.csv",
                    "instrument-period-returns.csv",
                    "pnl-contribution.csv",
                )),
                *(f"chatgpt-export/charts/{name}" for name in (
                    "portfolio-composition.svg",
                    "positions-pnl.svg",
                    "positions-vs-benchmark.svg",
                    "instruments-period-returns.svg",
                    "pnl-contribution.svg",
                )),
            }
            self.assertEqual(names, expected)
            directory_names = {
                f"chatgpt-export/{path.relative_to(export)}"
                for path in export.rglob("*")
                if path.is_file()
            }
            self.assertEqual(names, directory_names)
            self.assertTrue(all("__MACOSX" not in name and ".DS_Store" not in name for name in names))

    def test_validation_checks_schema_content_and_source_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "brokerage-current.json"
            source.write_text("{}\n", encoding="utf-8")
            model = dict(self.model)
            model["generated_from"] = [
                {
                    "path": "brokerage-current.json",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "role": "brokerage_snapshot",
                }
            ]
            write_outputs(root, model)

            self.assertEqual(validate_portfolio_outputs(root), [])

            summary = json.loads((root / "reports/chatgpt-export/market-summary.json").read_text(encoding="utf-8"))
            summary["metadata"].pop("purpose")
            (root / "reports/chatgpt-export/market-summary.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )
            self.assertIn("missing metadata purpose", validate_portfolio_outputs(root))

    def test_validation_rejects_quality_gaps_and_trading_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "brokerage-current.json"
            source.write_text("{}\n", encoding="utf-8")
            model = dict(self.model)
            model["generated_from"] = [
                {
                    "path": "brokerage-current.json",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "role": "brokerage_snapshot",
                }
            ]
            write_outputs(root, model)
            path = root / "reports/chatgpt-export/market-summary.json"
            baseline = json.loads(path.read_text(encoding="utf-8"))

            cases = [
                (
                    lambda value: value["metadata"].update(
                        {"calculated_at": "2026-08-15", "brokerage_snapshot_date": "2026-06-01"}
                    ),
                    "missing stale brokerage snapshot warning",
                ),
                (
                    lambda value: value["data_status"].update({"commissions": "confirmed_or_partial"}),
                    "missing partial commission warning",
                ),
                (
                    lambda value: value["positions"][0].update({"estimated_market_value_rub": None}),
                    "missing market quote warning: CASH",
                ),
                (
                    lambda value: value["benchmark_comparison"][0].update(
                        {"method": "weighted_entry_date", "limitations": []}
                    ),
                    "approximate benchmark row lacks limitation: CASH",
                ),
                (
                    lambda value: value["positions"][0].update({"recommendation": "buy"}),
                    "prohibited summary field: recommendation",
                ),
                (
                    lambda value: value["positions"][0].update({"action": "buy"}),
                    "prohibited trading action: buy",
                ),
            ]
            for mutate, expected in cases:
                with self.subTest(expected=expected):
                    candidate = json.loads(json.dumps(baseline))
                    mutate(candidate)
                    path.write_text(json.dumps(candidate), encoding="utf-8")
                    self.assertIn(expected, validate_portfolio_outputs(root))


if __name__ == "__main__":
    unittest.main()
