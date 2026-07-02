import csv
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).parents[1]


class MarketAnalysisCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data/market").mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "source_base_url": "https://iss.moex.com/iss",
            "instruments": [{"secid": "FUND", "type": "fund", "analysis_profile": "generic_fund", "enabled": True}],
        }
        (self.root / "data/market/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with (self.root / "data/market/FUND.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "board_id", "close", "price_unit", "accrued_interest", "unit_value_rub", "volume", "value_rub"])
            for day in range(1, 31):
                writer.writerow([f"2026-06-{day:02d}", "TQBR", 10 + day / 10, "rub_per_unit", "", 10 + day / 10, day, day * 1000])

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

    def test_check_does_not_create_zip(self):
        completed = self.run_cli("check-market-analysis")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((self.root / "reports/market-analysis.zip").exists())

    def test_export_creates_matching_directory_and_zip(self):
        completed = self.run_cli("export-market-analysis")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        export = self.root / "reports/market-analysis"
        self.assertTrue(export.exists())
        with zipfile.ZipFile(self.root / "reports/market-analysis.zip") as archive:
            names = set(archive.namelist())
            self.assertIn("market-analysis/analysis.md", names)
            self.assertIn("market-analysis/market-analysis.json", names)
            self.assertIn("market-analysis/data/FUND.csv", names)
            self.assertIn("market-analysis/charts/FUND-3m.svg", names)
            self.assertEqual(len([name for name in names if name.endswith(".svg")]), 4)
        directory_names = {
            f"market-analysis/{path.relative_to(export)}"
            for path in export.rglob("*")
            if path.is_file()
        }
        self.assertEqual(names, directory_names)

    def test_no_enabled_instruments_fails_without_traceback(self):
        (self.root / "data/market/manifest.json").write_text(
            json.dumps({"schema_version": 1, "instruments": []}), encoding="utf-8"
        )

        completed = self.run_cli("check-market-analysis")

        self.assertEqual(completed.returncode, 1)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
