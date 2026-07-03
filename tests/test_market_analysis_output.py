import json
import re
import unittest
from datetime import date, timedelta

from investment_tracker.market_analysis_calculations import build_instrument_analysis
from investment_tracker.market_analysis_output import (
    analysis_profile_notes,
    collect_missing_data,
    render_analysis_markdown,
    render_analytical_csv,
    render_market_chart,
    serializable_summary,
)


def rows(instrument_type="fund", count=90):
    board = {"fund": "TQBR", "bond": "TQOB", "reference": "CETS"}[instrument_type]
    result = []
    for index in range(count):
        row = {
            "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "board_id": board,
            "close": 100 + index / 10,
            "unit_value_rub": 100 + index / 10 if instrument_type != "bond" else 1030 + index,
            "accrued_interest": None if instrument_type != "bond" else 30 + index / 10,
        }
        if instrument_type == "reference":
            # A gold reference carries no turnover series (see EXPECTED_SERIES).
            row["volume"] = None
            row["value_rub"] = None
        else:
            row["volume"] = 100 + index
            row["value_rub"] = 10000 + index * 100
        if instrument_type == "bond":
            row["yield_close"] = 15 - index / 100
        result.append(row)
    return result


def panel_blocks(svg):
    """Return {panel_id: inner_markup} for each <g id="...-panel"> ... </g>,
    balancing nested <g> groups (clip groups, tick groups)."""
    blocks = {}
    for opening in re.finditer(r'<g id="([^"]*-panel)">', svg):
        panel_id = opening.group(1)
        start = opening.end()
        depth = 1
        for token in re.finditer(r"<g\b|</g>", svg[start:]):
            if token.group() == "</g>":
                depth -= 1
                if depth == 0:
                    blocks[panel_id] = svg[start:start + token.start()]
                    break
            else:
                depth += 1
    return blocks


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

    def test_gold_reference_has_explicit_profile_and_no_turnover_gap(self):
        reference = build_instrument_analysis(
            {"secid": "GLDRUB_TOM", "type": "reference", "analysis_profile": "gold_reference"},
            rows("reference"),
        )
        notes = analysis_profile_notes("gold_reference")
        absent = [r for r in collect_missing_data([reference]) if r["kind"] == "absent_series"]

        self.assertIn("золота", " ".join(notes["focus"]))
        self.assertEqual(absent, [])

    def test_summary_excludes_internal_rows(self):
        summary = serializable_summary(self.model)
        encoded = json.dumps(summary, ensure_ascii=False)

        self.assertNotIn("data_rows", encoded)
        self.assertNotIn('"rows"', encoded)
        self.assertIn("range_position_bucket", encoded)

    def test_name_reaches_summary_and_markdown(self):
        named = build_instrument_analysis(
            {"secid": "SBGD", "name": "Первая — Фонд Доступное золото", "type": "fund"},
            rows(),
        )
        model = {**self.model, "instruments": [named]}

        summary = serializable_summary(model)
        markdown = render_analysis_markdown(model)

        self.assertEqual(summary["instruments"][0]["name"], "Первая — Фонд Доступное золото")
        self.assertIn("## SBGD — Первая — Фонд Доступное золото", markdown)

    def test_name_falls_back_to_secid_and_heading_stays_bare(self):
        # Instruments without a manifest name (hand-written manifests, older data)
        # still get a usable label, and the markdown heading does not read "FUND — FUND".
        summary = serializable_summary(self.model)
        markdown = render_analysis_markdown(self.model)

        self.assertEqual(summary["instruments"][0]["name"], "FUND")
        self.assertIn("## FUND\n", markdown)
        self.assertNotIn("FUND — FUND", markdown)

    def test_missing_data_lists_partial_windows_and_excludes_covered_ones(self):
        # 200 observations from 2026-01-01 cover the 3m window fully but not 1y/5y.
        long_fund = build_instrument_analysis({"secid": "LONG", "type": "fund"}, rows(count=200))

        records = collect_missing_data([long_fund])

        partial = {r["window"] for r in records if r["kind"] == "partial_window"}
        self.assertEqual(partial, {"5y", "1y"})
        one_year = next(r for r in records if r["kind"] == "partial_window" and r["window"] == "1y")
        self.assertLess(one_year["requested_start"], one_year["actual_start"])

    def test_missing_data_flags_absent_expected_series(self):
        bond_rows = rows("bond")
        for row in bond_rows:
            row.pop("yield_close")
        no_yield = build_instrument_analysis({"secid": "OFZ", "type": "bond"}, bond_rows)

        absent = {r["field"] for r in collect_missing_data([no_yield]) if r["kind"] == "absent_series"}

        self.assertIn("yield_close_percent", absent)

    def test_missing_data_ignores_fully_populated_series(self):
        # The bond fixture carries both yields and turnover, so no absent_series record.
        absent = [r for r in collect_missing_data([self.bond]) if r["kind"] == "absent_series"]

        self.assertEqual(absent, [])

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
        self.assertIn('stroke="#2563eb"', svg)
        self.assertNotIn("MA20", svg)
        self.assertNotIn("stroke-dasharray", svg)
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
        # The yield panel is a framed sub-chart (baseline + y-axis + gridlines),
        # not a bare line floating below the price chart.
        self.assertIn('<line x1="90" y1="720" x2="940" y2="720"', svg)
        self.assertIn('<line x1="90" y1="520" x2="90" y2="720"', svg)

    def test_every_panel_with_a_line_has_axis_ticks(self):
        # The flaw that shipped: a panel drew a data line but no axis, so the line
        # floated "outside" any chart. Any panel that plots a <polyline> must also
        # draw value ticks — an unframed line fails here instead of slipping past a
        # human glance.
        for analysis, window in ((self.bond, "1y"), (self.fund, "3m"), (self.bond, "all")):
            svg = render_market_chart(analysis, window)
            panels = panel_blocks(svg)
            self.assertTrue(panels, "no chart panels found")
            for panel_id, inner in panels.items():
                if "<polyline" in inner:
                    self.assertIn(
                        'class="y-tick"',
                        inner,
                        f"{panel_id} ({analysis['secid']} {window}) plots a line but has no y-axis ticks",
                    )


if __name__ == "__main__":
    unittest.main()
