"""Geometry invariants for the generated SVG charts.

These tests catch the class of rendering bug that substring assertions miss:
legend/labels rendered off-canvas or crammed into a margin, plotted lines that
spill outside the plot area, and many series collapsed onto too few colors.
They exercise the renderers directly with multi-series / edge-case input, since
the single-instrument mock workspace never produces those shapes.
"""

from __future__ import annotations

import re
import unittest

from investment_tracker.performance_render import (
    _MULTI_LINE_PALETTE,
    render_bar_chart,
    render_multi_line_chart,
    render_period_returns_chart,
)

# Generous per-character width for 12px sans-serif; real glyphs are narrower, so
# staying under this bound guarantees the text is comfortably inside the canvas.
CHAR_WIDTH = 7.0


def svg_size(svg: str) -> tuple[int, int]:
    match = re.search(r'<svg[^>]*\bwidth="(\d+)"[^>]*\bheight="(\d+)"', svg)
    assert match, "svg has no width/height"
    return int(match.group(1)), int(match.group(2))


def texts(svg: str) -> list[tuple[float, float, str, bool, str]]:
    """(x, y, anchor, rotated, content) for every <text> element."""
    result = []
    for match in re.finditer(r'<text x="([\d.-]+)" y="([\d.-]+)"([^>]*)>([^<]*)</text>', svg):
        x, y, attrs, content = match.groups()
        anchor_match = re.search(r'text-anchor="(\w+)"', attrs)
        anchor = anchor_match.group(1) if anchor_match else "start"
        rotated = "transform=" in attrs
        result.append((float(x), float(y), anchor, rotated, content))
    return result


def polyline_points(svg: str) -> list[tuple[float, float]]:
    points = []
    for match in re.finditer(r'<polyline[^>]*\bpoints="([^"]+)"', svg):
        for pair in match.group(1).split():
            xs, ys = pair.split(",")
            points.append((float(xs), float(ys)))
    return points


def polyline_colors(svg: str) -> list[str]:
    return re.findall(r'<polyline[^>]*\bstroke="([^"]+)"', svg)


class ChartGeometryTests(unittest.TestCase):
    def assert_text_in_canvas(self, svg: str) -> None:
        width, height = svg_size(svg)
        for x, y, anchor, rotated, content in texts(svg):
            self.assertGreaterEqual(x, 0, f"text {content!r} x<0")
            self.assertLessEqual(x, width, f"text {content!r} x past right edge")
            self.assertGreaterEqual(y, 0, f"text {content!r} y<0")
            self.assertLessEqual(y, height, f"text {content!r} y past bottom edge")
            if rotated or not content:
                continue  # rotated-text extent is not axis-aligned; skip extent check
            span = len(content) * CHAR_WIDTH
            if anchor == "start":
                right = x + span
            elif anchor == "end":
                right = x
                self.assertGreaterEqual(x - span, -1, f"text {content!r} runs off left edge")
            else:  # middle
                right = x + span / 2
                self.assertGreaterEqual(x - span / 2, -1, f"text {content!r} runs off left edge")
            self.assertLessEqual(right, width + 1, f"text {content!r} runs off right edge")

    def assert_polylines_in_canvas(self, svg: str) -> None:
        width, height = svg_size(svg)
        for x, y in polyline_points(svg):
            self.assertTrue(0 <= x <= width, f"polyline x={x} outside [0,{width}]")
            self.assertTrue(0 <= y <= height, f"polyline y={y} outside [0,{height}]")

    def _sample_series(self, count: int) -> dict[str, list[dict]]:
        # Long, realistic labels (a shared benchmark drawn once) across the count.
        names = [
            "SBFR", "SBGD", "SBMM", "SBRB", "SU26244RMFS2",
            "SBHI", "SBCB", "AKME", "TMOS", "EQMX", "SBGB", "OBLG",
        ][:count]
        series = {}
        for index, name in enumerate(names):
            series[name] = [
                {"date": "2026-05-10", "normalized": 100.0},
                {"date": "2026-06-10", "normalized": 100.0 + index - 2},
                {"date": "2026-07-01", "normalized": 100.0 + (index - 2) * 1.5},
            ]
        return series

    def test_multi_line_legend_and_lines_stay_in_canvas(self):
        for count in (2, 5, len(_MULTI_LINE_PALETTE)):
            svg = render_multi_line_chart(
                "Instrument performance vs benchmark (indexed to 100)",
                self._sample_series(count),
                "normalized",
            )
            self.assert_text_in_canvas(svg)
            self.assert_polylines_in_canvas(svg)

    def test_multi_line_colors_are_distinct_within_palette(self):
        count = len(_MULTI_LINE_PALETTE)
        svg = render_multi_line_chart("t", self._sample_series(count), "normalized")
        colors = polyline_colors(svg)
        self.assertEqual(len(colors), count)
        self.assertEqual(len(set(colors)), count, "series share colors — indistinguishable lines")

    def test_bar_chart_rotated_labels_stay_in_canvas(self):
        # Composed labels like the real benchmark chart, which trigger rotation.
        categories = [
            (f"TICKER{i} (1 234 567.89 RUB)", (i - 3) * 12345.67) for i in range(7)
        ]
        svg = render_bar_chart("Confirmed PnL contribution", categories, "RUB")
        self.assert_text_in_canvas(svg)
        self.assert_polylines_in_canvas(svg)

    def test_period_returns_table_stays_in_canvas(self):
        rows = [
            {"ticker": t, "returns_pct": {"1m": 0.01, "3m": -0.02, "6m": 0.03, "12m": None, "ytd": 0.05}}
            for t in ("SBFR", "SBGD", "SBMM", "SBRB", "SU26244RMFS2")
        ]
        svg = render_period_returns_chart(rows)
        self.assert_text_in_canvas(svg)


if __name__ == "__main__":
    unittest.main()
