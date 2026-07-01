import json
import tempfile
import unittest
from pathlib import Path

from investment_tracker.market_data import (
    MarketDataError,
    MoexClient,
    add_instrument,
    adjust_history_for_corporate_actions,
    default_analysis_profile,
    history_overlap,
    load_manifest,
    merge_board_rows,
    normalize_history_row,
    read_market_csv,
    select_history_boards,
    update_instrument,
    write_market_csv,
)


class FakeClient:
    def get(self, path, params=None):
        if path.startswith("securities/"):
            return {
                "description": {"columns": ["name", "value"], "data": [["NAME", "X"], ["ISIN", "X"]]},
                "boards": {
                    "columns": ["boardid", "engine", "market", "is_primary"],
                    "data": [["TQBR", "stock", "shares", 1]],
                },
            }
        return {
            "history": {"columns": ["TRADEDATE", "CLOSE", "VOLUME", "VALUE"], "data": [["2026-06-30", 10, 1, 10]]},
            "history.cursor": {"columns": ["TOTAL"], "data": [[1]]},
        }


class MarketDataTests(unittest.TestCase):
    def test_normalizes_bond_yield_close(self):
        row = normalize_history_row(
            {
                "TRADEDATE": "2026-06-30",
                "CLOSE": 82.51,
                "YIELDCLOSE": 15.74,
                "ACCINT": 29.9,
                "FACEVALUE": 1000,
            },
            board_id="TQOB",
            instrument_type="bond",
        )

        self.assertEqual(row["yield_close"], 15.74)

    def test_reads_legacy_fund_and_extended_bond_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            fund = Path(directory) / "FUND.csv"
            fund.write_text(
                "date,board_id,close,price_unit,accrued_interest,unit_value_rub,volume,value_rub\n"
                "2026-06-30,TQBR,10,rub_per_unit,,10,1,10\n",
                encoding="utf-8",
            )
            bond = Path(directory) / "BOND.csv"
            bond.write_text(
                "date,board_id,close,price_unit,accrued_interest,unit_value_rub,volume,value_rub,yield_close\n"
                "2026-06-30,TQOB,82.51,percent_of_nominal,29.9,855,1,825.1,15.74\n",
                encoding="utf-8",
            )

            self.assertNotIn("yield_close", read_market_csv(fund)[0])
            self.assertEqual(read_market_csv(bond)[0]["yield_close"], 15.74)

    def test_bond_without_yield_column_requires_full_backfill(self):
        legacy = [{"date": "2026-06-30", "close": 82.51}]
        extended = [{"date": "2026-06-30", "close": 82.51, "yield_close": None}]

        self.assertIsNone(history_overlap(legacy, {"type": "bond"}))
        self.assertEqual(history_overlap(extended, {"type": "bond"}), "2026-06-23")

    def test_default_analysis_profiles_are_generic_and_safe(self):
        self.assertEqual(default_analysis_profile("fund"), "generic_fund")
        self.assertEqual(default_analysis_profile("bond"), "generic_bond")

    def test_adjusts_pre_split_prices_to_current_units(self):
        rows = [
            {"date": "2021-05-31", "close": 1127.4, "unit_value_rub": 1127.4},
            {"date": "2021-06-07", "close": 11.28, "unit_value_rub": 11.28},
        ]

        adjusted = adjust_history_for_corporate_actions(
            rows,
            [{"type": "split", "effective_date": "2021-06-04", "ratio": 100}],
        )

        self.assertAlmostEqual(adjusted[0]["close"], 11.274)
        self.assertAlmostEqual(adjusted[0]["unit_value_rub"], 11.274)
        self.assertEqual(adjusted[1]["unit_value_rub"], 11.28)
        self.assertEqual(rows[0]["unit_value_rub"], 1127.4)

    def test_stitches_board_migration_without_duplicate_dates(self):
        rows, warnings = merge_board_rows(
            [
                [{"date": "2026-06-19", "board_id": "TQTF", "close": 18.72}],
                [{"date": "2026-06-22", "board_id": "TQBR", "close": 18.78}],
            ],
            primary_board="TQBR",
        )

        self.assertEqual([row["date"] for row in rows], ["2026-06-19", "2026-06-22"])
        self.assertEqual(warnings, [])

    def test_primary_board_wins_overlap_and_warns_on_conflict(self):
        rows, warnings = merge_board_rows(
            [
                [{"date": "2026-06-22", "board_id": "TQTF", "close": 18.70}],
                [{"date": "2026-06-22", "board_id": "TQBR", "close": 18.78}],
            ],
            primary_board="TQBR",
        )

        self.assertEqual(rows[0]["board_id"], "TQBR")
        self.assertEqual(len(warnings), 1)

    def test_richer_row_replaces_legacy_row_on_same_board(self):
        rows, warnings = merge_board_rows(
            [
                [{"date": "2026-06-30", "board_id": "TQOB", "close": 82.51}],
                [
                    {
                        "date": "2026-06-30",
                        "board_id": "TQOB",
                        "close": 82.51,
                        "yield_close": 15.74,
                    }
                ],
            ],
            primary_board="TQOB",
        )

        self.assertEqual(rows[0]["yield_close"], 15.74)
        self.assertEqual(warnings, [])

    def test_normalizes_bond_dirty_price(self):
        row = normalize_history_row(
            {
                "TRADEDATE": "2026-06-29",
                "CLOSE": 82.82,
                "ACCINT": 29.59,
                "FACEVALUE": 1000,
                "VOLUME": 500452,
                "VALUE": 413782048.76,
            },
            board_id="TQOB",
            instrument_type="bond",
        )

        self.assertAlmostEqual(row["unit_value_rub"], 857.79)
        self.assertEqual(row["price_unit"], "percent_of_nominal")

    def test_missing_bond_accrued_interest_is_rejected(self):
        with self.assertRaises(MarketDataError):
            normalize_history_row(
                {"TRADEDATE": "2026-06-29", "CLOSE": 82.82, "FACEVALUE": 1000},
                board_id="TQOB",
                instrument_type="bond",
            )

    def test_no_trade_history_row_is_ignored(self):
        row = normalize_history_row(
            {"TRADEDATE": "2022-01-07", "CLOSE": None, "VOLUME": 0, "VALUE": 0},
            board_id="TQTF",
            instrument_type="fund",
        )

        self.assertIsNone(row)

    def test_bond_history_uses_only_primary_board(self):
        security = {"primary_board": "TQOB", "boards": ["PACT", "TQOB"]}

        self.assertEqual(select_history_boards(security, "bond"), ["TQOB"])

    def test_fund_history_keeps_old_and_current_main_boards(self):
        security = {"primary_board": "TQBR", "boards": ["PACT", "TQBR", "TQTF"]}

        self.assertEqual(select_history_boards(security, "fund"), ["TQBR", "TQTF"])

    def test_non_numeric_csv_cell_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "BAD.csv"
            path.write_text(
                "date,board_id,close,price_unit,accrued_interest,unit_value_rub,volume,value_rub\n"
                "2026-06-30,TQBR,not-a-number,rub_per_unit,,10,1,10\n",
                encoding="utf-8",
            )

            with self.assertRaises(MarketDataError):
                read_market_csv(path)

    def test_atomic_csv_write_is_idempotent(self):
        rows = [
            {
                "date": "2026-06-22",
                "board_id": "TQBR",
                "close": 18.78,
                "price_unit": "rub_per_unit",
                "accrued_interest": None,
                "unit_value_rub": 18.78,
                "volume": 10,
                "value_rub": 368352400.27,
            },
            {
                "date": "2026-06-19",
                "board_id": "TQTF",
                "close": 18.72,
                "price_unit": "rub_per_unit",
                "accrued_interest": None,
                "unit_value_rub": 18.72,
                "volume": 20,
                "value_rub": 374.4,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "MOCK.csv"
            write_market_csv(path, rows)
            first = path.read_bytes()
            write_market_csv(path, list(reversed(rows)))

            self.assertEqual(path.read_bytes(), first)
            self.assertEqual([row["date"] for row in read_market_csv(path)], ["2026-06-19", "2026-06-22"])
            self.assertIn(",368352400.27\n", path.read_text(encoding="utf-8"))


class WorkspaceBoundaryTests(unittest.TestCase):
    def _make_workspace(self, directory, secid="FUND"):
        root = Path(directory)
        (root / "data/market").mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "source": "MOEX ISS",
            "instruments": [
                {"secid": secid, "type": "fund", "benchmark": "CASH", "enabled": True}
            ],
        }
        (root / "data/market/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_add_instrument_rejects_secid_with_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            (root / "data/market").mkdir(parents=True)
            (root / "data/market/manifest.json").write_text(
                json.dumps({"schema_version": 1, "instruments": []}), encoding="utf-8"
            )
            escaped = Path(directory) / "ESCAPED.csv"

            with self.assertRaises(MarketDataError):
                add_instrument(FakeClient(), root, "../../ESCAPED", "fund", "CASH")

            self.assertFalse(escaped.exists())

    def test_update_instrument_rejects_secid_with_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            (root / "data/market").mkdir(parents=True)
            escaped = Path(directory) / "ESCAPED.csv"

            with self.assertRaises(MarketDataError):
                update_instrument(
                    FakeClient(),
                    root,
                    {"secid": "../../../ESCAPED", "type": "fund", "benchmark": "CASH"},
                )

            self.assertFalse(escaped.exists())

    def test_load_manifest_rejects_instrument_secid_with_separator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._make_workspace(directory, secid="../../evil")

            with self.assertRaises(MarketDataError):
                load_manifest(root / "data/market/manifest.json")

    def test_load_manifest_accepts_real_secids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._make_workspace(directory, secid="SU26238RMFS4")
            manifest = load_manifest(root / "data/market/manifest.json")

            self.assertEqual(manifest["instruments"][0]["secid"], "SU26238RMFS4")

    def test_add_instrument_accepts_normal_secid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/market").mkdir(parents=True)
            (root / "data/market/manifest.json").write_text(
                json.dumps({"schema_version": 1, "instruments": []}), encoding="utf-8"
            )

            add_instrument(FakeClient(), root, "SBER", "fund", "CASH")

            self.assertTrue((root / "data/market/SBER.csv").exists())


class MoexClientTests(unittest.TestCase):
    def test_rejects_non_http_base_url(self):
        with self.assertRaises(MarketDataError):
            MoexClient(base_url="file:///etc/passwd")

    def test_rejects_ftp_base_url(self):
        with self.assertRaises(MarketDataError):
            MoexClient(base_url="ftp://example.com/data")

    def test_accepts_http_and_https_base_url(self):
        self.assertEqual(MoexClient(base_url="http://example.com/iss").base_url, "http://example.com/iss")
        self.assertEqual(
            MoexClient(base_url="https://iss.moex.com/iss").base_url, "https://iss.moex.com/iss"
        )


if __name__ == "__main__":
    unittest.main()
