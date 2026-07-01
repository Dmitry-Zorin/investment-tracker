import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from investment_tracker.cli import build_parser


PACKAGE_ROOT = Path(__file__).parents[1]


class MarketReportCliTests(unittest.TestCase):
    def test_add_accepts_analysis_profile(self):
        args = build_parser().parse_args(
            [
                "--workspace",
                "/tmp/example",
                "add",
                "TEST",
                "--type",
                "fund",
                "--benchmark",
                "CASH",
                "--analysis-profile",
                "gold_fund",
            ]
        )

        self.assertEqual(args.analysis_profile, "gold_fund")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data/market").mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "source": "MOEX ISS",
            "source_base_url": "https://iss.moex.com/iss",
            "instruments": [
                {"secid": "FUND", "instrument_id": "F1", "type": "fund", "benchmark": "FUND", "enabled": True}
            ],
        }
        (self.root / "data/market/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with (self.root / "data/market/FUND.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "board_id", "close", "price_unit", "accrued_interest", "unit_value_rub", "volume", "value_rub"])
            writer.writerow(["2020-01-01", "TQTF", "10", "rub_per_unit", "", "10", "1", "10"])
            writer.writerow(["2020-01-02", "TQTF", "11", "rub_per_unit", "", "11", "1", "11"])
        (self.root / "brokerage-ledger.jsonl").write_text(
            json.dumps({"record_type": "schema", "schema_version": 1}) + "\n", encoding="utf-8"
        )
        brokerage = {"as_of": "2020-01-02", "positions": [], "warnings": [], "checks": [{"status": "pass"}]}
        (self.root / "brokerage-current.json").write_text(json.dumps(brokerage), encoding="utf-8")
        self.authoritative = [
            "brokerage-current.json",
            "brokerage-ledger.jsonl",
            "data/market/manifest.json",
            "data/market/FUND.csv",
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, command):
        return subprocess.run(
            [sys.executable, "-m", "investment_tracker", "--workspace", str(self.root), command],
            cwd=PACKAGE_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_build_does_not_modify_authoritative_files(self):
        before = {name: (self.root / name).read_bytes() for name in self.authoritative}

        completed = self.run_cli("build")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        after = {name: (self.root / name).read_bytes() for name in self.authoritative}
        self.assertEqual(after, before)
        self.assertTrue((self.root / "reports/performance.md").exists())

    def test_check_reports_stale_market_data(self):
        completed = self.run_cli("check")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("stale", completed.stderr.lower())

    def test_build_reports_no_enabled_instruments_without_traceback(self):
        (self.root / "data/market/manifest.json").write_text(
            json.dumps({"schema_version": 1, "instruments": []}), encoding="utf-8"
        )

        completed = self.run_cli("build")

        self.assertEqual(completed.returncode, 1)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_build_reports_malformed_position_without_traceback(self):
        brokerage = {
            "as_of": "2020-01-02",
            "positions": [
                {
                    "account_id": "mock-account",
                    "instrument_id": "F1",
                    "name": "Fund",
                    "asset_type": "money_market_fund",
                    "quantity": 1,
                    "first_trade_date": "2020-01-01",
                    "cost_basis": 10,
                    "current_value_without_nkd": 10,
                    "current_nkd": 0,
                    "total_pnl": 0,
                    "return": 0,
                }
            ],
            "warnings": [],
            "checks": [{"status": "pass"}],
        }
        (self.root / "brokerage-current.json").write_text(json.dumps(brokerage), encoding="utf-8")

        completed = self.run_cli("build")

        self.assertEqual(completed.returncode, 1)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIn("current_value", completed.stderr)

    def test_export_contains_required_package(self):
        self.assertEqual(self.run_cli("build").returncode, 0)

        completed = self.run_cli("export-chatgpt")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        export = self.root / "reports/chatgpt-export"
        self.assertFalse(export.exists())
        self.assertTrue((self.root / "reports/chatgpt-export.zip").exists())
        self.assertIn("reports/chatgpt-export.zip", completed.stdout)


if __name__ == "__main__":
    unittest.main()
