from __future__ import annotations

import html
import math
from datetime import date

from investment_tracker.performance_model import OutputError


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}".replace(",", " ")


def _fmt_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _md_cell(value: object) -> str:
    # Neutralize characters that would break a Markdown table row: escape the
    # column separator and collapse newlines. brokerage-supplied free-form
    # strings (name, account id, asset type) are otherwise interpolated raw.
    return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def render_performance_report(model: dict) -> str:
    corporate_action_notes = [
        f"{action['secid']}: цены до {action['effective_date']} скорректированы на дробление "
        f"1:{action['ratio']:g} для сопоставимости с текущим паем (источник: {action['source']})."
        for action in model.get("corporate_actions", [])
        if action.get("type") == "split"
    ]
    portfolio = model["portfolio"]
    contributions = model.get("pnl_contribution", [])
    positive = max(
        (item for item in contributions if item["pnl_rub"] > 0),
        key=lambda item: item["pnl_rub"],
        default=None,
    )
    negative = min(
        (item for item in contributions if item["pnl_rub"] < 0),
        key=lambda item: item["pnl_rub"],
        default=None,
    )
    better = [row["ticker"] for row in model.get("benchmark_comparison", []) if row["interpretation"] == "outperformed_benchmark"]
    worse = [row["ticker"] for row in model.get("benchmark_comparison", []) if row["interpretation"] == "underperformed_benchmark"]
    benchmark_label = ", ".join(model.get("benchmarks", [])) or "not configured"
    lines = [
        "# Portfolio Performance Report",
        "",
        "## 1. Metadata",
        "",
        f"- Calculation date: {model['calculated_at']}",
        f"- Brokerage snapshot date: {model['brokerage_snapshot_date']}",
        f"- Last public market quote date: {model['latest_market_date']}",
        "- Currency: RUB",
        f"- Configured benchmark(s): {benchmark_label}",
        "",
        "## 2. Purpose and scope",
        "",
        "Purpose: portfolio performance control and instrument comparison.",
        "",
        "Not for: entry/exit timing, technical analysis, buy/sell signals, market zone analysis.",
        "",
        "Timing and market-zone analysis is handled by a separate market-analysis.zip package.",
        "",
        "## 3. Executive summary",
        "",
        f"- Total brokerage portfolio value including cash: {_fmt_money(portfolio['confirmed_value_rub'] + portfolio['cash_rub'])} RUB.",
        f"- Total open-position cost basis: {_fmt_money(portfolio['total_cost_basis_rub'])} RUB.",
        f"- Confirmed PnL: {_fmt_money(portfolio['confirmed_pnl_rub'])} RUB ({_fmt_percent(portfolio['confirmed_pnl_pct'])}).",
        f"- Largest positive contribution: {positive['ticker'] if positive else 'n/a'} ({_fmt_money(positive['pnl_rub']) if positive else 'n/a'} RUB).",
        f"- Largest negative contribution: {negative['ticker'] if negative else 'n/a'} ({_fmt_money(negative['pnl_rub']) if negative else 'n/a'} RUB).",
        f"- Better than configured benchmark: {', '.join(better) or 'none'}; worse: {', '.join(worse) or 'none'}.",
        "- Positions, quantities, cost basis, commissions and confirmed values come from the brokerage snapshot and ledger.",
        "- Latest public values, period returns and configured-benchmark comparisons are preliminary MOEX-based estimates; taxes and a personal bank-deposit benchmark are not calculated.",
        "",
        "## 4. Data status",
        "",
        "Confirmed by brokerage snapshot:",
        "- positions, quantities, average entry prices, cost basis and confirmed valuation;",
        f"- commissions: {model['data_status']['commissions']};",
        f"- accrued coupon income: {model['data_status']['accrued_coupon_income']}.",
        "",
        "Estimated from MOEX/public market data:",
        "- latest public market value;",
        "- period returns;",
        "- comparison against each instrument's configured benchmark;",
        "- unrealized market comparison.",
        "",
        "Not calculated:",
        "- taxes;",
        "- personal bank deposit benchmark;",
        "- intraday bid/ask spread;",
        "- entry/exit timing.",
        "",
        "## 5. Portfolio summary",
        "",
        "| Metric | Value | Currency | Source |",
        "| --- | ---: | --- | --- |",
        f"| Total cost basis | {_fmt_money(portfolio['total_cost_basis_rub'])} | RUB | brokerage snapshot |",
        f"| Confirmed value | {_fmt_money(portfolio['confirmed_value_rub'])} | RUB | brokerage snapshot |",
        f"| Estimated market value | {_fmt_money(portfolio['estimated_market_value_rub'])} | RUB | MOEX/latest quotes |",
        f"| Confirmed PnL | {_fmt_money(portfolio['confirmed_pnl_rub'])} | RUB | brokerage snapshot |",
        f"| Confirmed PnL | {_fmt_percent(portfolio['confirmed_pnl_pct'])} | percent | brokerage snapshot |",
        f"| Cash / unallocated brokerage balance | {_fmt_money(portfolio['cash_rub'])} | RUB | brokerage snapshot |",
        "",
        "## 6. Positions",
        "",
        "| Account | Ticker | Name | Asset class | Quantity | Cost basis | Confirmed value | PnL RUB | PnL % | First entry | Last entry | Commissions | ACI |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: |",
    ]
    for position in model["positions"]:
        lines.append(
            f"| {_md_cell(position.get('account_alias', position['account_id']))} | {_md_cell(position['ticker'])} | {_md_cell(position['name'])} | "
            f"{_md_cell(position.get('asset_class', position['asset_type']))} | {position['quantity']:g} | "
            f"{_fmt_money(position['cost_basis'])} | {_fmt_money(position['confirmed_value'])} | "
            f"{_fmt_money(position['confirmed_pnl'])} | {_fmt_percent(position['confirmed_return'])} | "
            f"{position.get('first_entry_date', position.get('first_trade_date'))} | {position.get('last_entry_date', 'n/a')} | "
            f"{_fmt_money(position.get('commissions_rub'))} | {_fmt_money(position.get('accrued_coupon_income_rub'))} |"
        )
    lines.extend(
        [
            "",
            "## 7. PnL contribution",
            "",
            "| Ticker | PnL RUB | PnL % | Share of total PnL | Portfolio impact |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in model.get("pnl_contribution", []):
        lines.append(
            f"| {row['ticker']} | {_fmt_money(row['pnl_rub'])} | {_fmt_percent(row['pnl_pct'])} | "
            f"{_fmt_percent(row['share_of_total_pnl_pct'])} | {_fmt_percent(row['portfolio_impact_pct'])} |"
        )
    lines.extend(
        [
            "",
            "Share of total PnL shows what part of the total result is explained by each instrument. Portfolio impact shows the effect on the whole brokerage portfolio.",
            "",
            "## 8. Comparison vs configured benchmark",
            "",
            "| Ticker | Benchmark | Entry date | Instrument return | Benchmark return same period | Difference p.p. | Result vs benchmark RUB | Interpretation |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in model.get("benchmark_comparison", []):
        difference_label = (
            "n/a" if row["difference_pct_points"] is None else f"{row['difference_pct_points']:.2f}"
        )
        lines.append(
            f"| {row['ticker']} | {row['benchmark_ticker']} | {row['entry_date']} | {_fmt_percent(row['instrument_return_since_entry_pct'])} | "
            f"{_fmt_percent(row['benchmark_return_same_period_pct'])} | "
            f"{difference_label} | "
            f"{_fmt_money(row['result_vs_benchmark_rub'])} | {row['interpretation']} |"
        )
    lines.extend(
        [
            "",
            "Benchmarks come from the market manifest. Comparisons are lot-based where transaction dates are available and are not trading signals.",
            "",
            "## 9. Instrument period returns",
            "",
            "| Ticker | Asset class | 1m | 3m | 6m | 12m | YTD | Last quote date |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in model.get("instrument_period_returns", []):
        returns = row["returns_pct"]
        lines.append(
            f"| {row['ticker']} | {row['asset_class']} | {_fmt_percent(returns['1m'])} | "
            f"{_fmt_percent(returns['3m'])} | {_fmt_percent(returns['6m'])} | {_fmt_percent(returns['12m'])} | "
            f"{_fmt_percent(returns['ytd'])} | {row['last_quote_date']} |"
        )
    lines.extend(
        [
            "",
            "Period returns are included only to describe instrument behavior. They are not used as entry/exit timing signals in this package.",
            "",
            "## 10. Charts",
            "",
            "- `portfolio-composition.svg` — portfolio allocation by ticker, including brokerage cash.",
            "- `positions-pnl.svg` — confirmed current PnL by position.",
            "- `positions-vs-benchmark.svg` — lot-based result difference versus each configured benchmark.",
            "- `instruments-period-returns.svg` — 1m/3m/6m/12m/YTD returns by instrument.",
            "- `pnl-contribution.svg` — confirmed contribution of each instrument to total PnL.",
            "- `instruments-vs-benchmark.svg` — each instrument and its benchmark indexed to 100 over the holding period.",
            "",
            "## 11. Data limitations",
            "",
        ]
    )
    lines.extend(
        [f"- [{item['severity']}] {item['code']}: {item['message']}" for item in model.get("warnings", [])]
        or ["- No source warnings."]
    )
    lines.extend([f"- Missing data: {item}" for item in model.get("missing_data", [])])
    lines.extend(f"- {note}" for note in corporate_action_notes)
    lines.extend(
        [
            "- Taxes are not calculated.",
            "- A personal bank-deposit benchmark is unavailable without exact user terms.",
            "- Intraday bid/ask spreads are not included.",
            "",
            "## 12. Not included in this package",
            "",
            "This package intentionally does not include:",
            "- entry/exit timing;",
            "- support/resistance levels;",
            "- RSI, MACD, Bollinger Bands or similar indicators;",
            "- MA20/MA60 market-zone analysis;",
            "- drawdown-based entry zones;",
            "- central bank rate scenarios;",
            "- buy/sell/hold recommendations;",
            "- intraday order book spreads;",
            "- personal bank deposit modeling without exact user terms.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_bar_chart(title: str, categories: list[tuple[str, float]], unit: str) -> str:
    if not categories:
        raise OutputError(f"At least one category is required for chart {title}")
    width, height, left, right, top = 900, 450, 70, 25, 45
    slot = (width - left - right) / len(categories)
    # Rotate the x-axis labels when a horizontal label would not fit its slot,
    # so composed labels like "TICKER (1 234 567.89 RUB)" stop overlapping once
    # there are several categories; reserve extra bottom margin when rotated.
    longest_label = max((len(str(label)) for label, _ in categories), default=0)
    rotate_labels = longest_label * 7 > slot
    bottom = 150 if rotate_labels else 60
    values = [float(value) for _, value in categories]
    minimum, maximum = min(0.0, min(values)), max(0.0, max(values))
    if minimum == maximum:
        maximum = minimum + 1
    plot_height = height - top - bottom
    plot_bottom = height - bottom

    def y(value: float) -> float:
        return top + (maximum - value) * plot_height / (maximum - minimum)

    baseline = y(0)
    bar_width = min(100, slot * 0.55)
    elements = []
    for index, (label, value) in enumerate(categories):
        center = left + slot * (index + 0.5)
        value_y = y(float(value))
        bar_y = min(value_y, baseline)
        bar_height = max(1, abs(baseline - value_y))
        color = "#2563eb" if value >= 0 else "#dc2626"
        elements.append(
            f'<rect class="bar" x="{center-bar_width/2:.2f}" y="{bar_y:.2f}" width="{bar_width:.2f}" height="{bar_height:.2f}" fill="{color}"/>'
        )
        label_y = plot_bottom + 16
        escaped_label = html.escape(str(label))
        if rotate_labels:
            elements.append(
                f'<text x="{center:.2f}" y="{label_y:.2f}" '
                f'transform="rotate(-35 {center:.2f} {label_y:.2f})" '
                f'text-anchor="end" font-family="sans-serif" font-size="12">{escaped_label}</text>'
            )
        else:
            elements.append(
                f'<text x="{center:.2f}" y="{label_y:.2f}" text-anchor="middle" font-family="sans-serif" font-size="12">{escaped_label}</text>'
            )
        elements.append(
            f'<text x="{center:.2f}" y="{bar_y-6:.2f}" text-anchor="middle" font-family="sans-serif" font-size="12">{_fmt_money(value)}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        '<rect width="100%" height="100%" fill="#ffffff"/>\n'
        f'<text x="{left}" y="28" font-family="sans-serif" font-size="18">{html.escape(title)}</text>\n'
        f'<line x1="{left}" y1="{top:.2f}" x2="{left}" y2="{plot_bottom:.2f}" stroke="#555"/>\n'
        f'<text x="8" y="{top+8:.2f}" font-family="sans-serif" font-size="11">{_fmt_money(maximum)}</text>\n'
        f'<text x="8" y="{plot_bottom:.2f}" font-family="sans-serif" font-size="11">{_fmt_money(minimum)}</text>\n'
        f'<line x1="{left}" y1="{baseline:.2f}" x2="{width-right}" y2="{baseline:.2f}" stroke="#555"/>\n'
        + "\n".join(elements)
        + "\n"
        + f'<text x="{left}" y="{height-8}" font-family="sans-serif" font-size="11">Source: brokerage-current.json and MOEX ISS. Values: {html.escape(unit)}.</text>\n'
        + '</svg>\n'
    )


def render_period_returns_chart(rows: list[dict]) -> str:
    periods = (("1m", "1m"), ("3m", "3m"), ("6m", "6m"), ("12m", "12m"), ("ytd", "YTD"))
    width = 820
    row_height = 42
    top = 82
    height = top + max(1, len(rows)) * row_height + 52
    label_width = 180
    cell_width = 118
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="24" y="28" font-family="sans-serif" font-size="18">Instrument period returns</text>',
    ]
    for index, (_, label) in enumerate(periods):
        x = label_width + index * cell_width + cell_width / 2
        elements.append(
            f'<text x="{x:.1f}" y="62" text-anchor="middle" font-family="sans-serif" font-size="12">{label}</text>'
        )
    usable_rows = rows or [{"ticker": "not available", "returns_pct": {key: None for key, _ in periods}}]
    for row_index, row in enumerate(usable_rows):
        y = top + row_index * row_height
        elements.append(
            f'<text x="24" y="{y + 25}" font-family="sans-serif" font-size="12">{html.escape(row["ticker"])}</text>'
        )
        for column, (key, _) in enumerate(periods):
            value = row["returns_pct"].get(key)
            x = label_width + column * cell_width
            color = "#e5e7eb" if value is None else ("#dcfce7" if value >= 0 else "#fee2e2")
            label = "n/a" if value is None else f"{value * 100:.2f}%"
            elements.append(
                f'<rect x="{x + 3}" y="{y + 3}" width="{cell_width - 6}" height="{row_height - 6}" fill="{color}"/>'
            )
            elements.append(
                f'<text x="{x + cell_width / 2:.1f}" y="{y + 25}" text-anchor="middle" font-family="sans-serif" font-size="12">{label}</text>'
            )
    elements.append(
        f'<text x="24" y="{height - 14}" font-family="sans-serif" font-size="11">Source: MOEX market data. Descriptive only; not a timing signal.</text>'
    )
    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def render_multi_line_chart(title: str, series: dict[str, list[dict]], value_key: str) -> str:
    width, height = 900, 450
    left, right, top, bottom = 70, 170, 45, 75
    usable = {label: rows for label, rows in series.items() if len(rows) >= 2}
    if not usable:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
            '<rect width="100%" height="100%" fill="#ffffff"/>\n'
            f'<text x="{left}" y="28" font-family="sans-serif" font-size="18">{html.escape(title)}</text>\n'
            f'<text x="{left}" y="{height // 2}" font-family="sans-serif" font-size="14">not available</text>\n'
            '</svg>\n'
        )
    all_dates = [date.fromisoformat(row["date"]) for rows in usable.values() for row in rows]
    all_values = [float(row[value_key]) for rows in usable.values() for row in rows]
    if not all(math.isfinite(value) for value in all_values):
        raise OutputError(f"Non-finite chart value for {title}")
    first_date, last_date = min(all_dates), max(all_dates)
    minimum, maximum = min(all_values), max(all_values)
    if minimum == maximum:
        minimum -= 1
        maximum += 1
    day_span = max(1, (last_date - first_date).days)

    def points(rows: list[dict]) -> str:
        result = []
        for row in rows:
            row_date = date.fromisoformat(row["date"])
            value = float(row[value_key])
            x = left + (row_date - first_date).days * (width - left - right) / day_span
            y = top + (maximum - value) * (height - top - bottom) / (maximum - minimum)
            result.append(f"{x:.2f},{y:.2f}")
        return " ".join(result)

    colors = ("#2563eb", "#d97706", "#059669", "#dc2626", "#7c3aed", "#0891b2")
    elements = []
    for index, (label, rows) in enumerate(sorted(usable.items())):
        color = colors[index % len(colors)]
        elements.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points(rows)}"/>')
        legend_y = top + index * 20
        elements.append(
            f'<line x1="{width-right+15}" y1="{legend_y}" x2="{width-right+35}" y2="{legend_y}" stroke="{color}" stroke-width="2"/>'
        )
        elements.append(
            f'<text x="{width-right+42}" y="{legend_y+4}" font-family="sans-serif" font-size="12">{html.escape(label)}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        '<rect width="100%" height="100%" fill="#ffffff"/>\n'
        f'<text x="{left}" y="28" font-family="sans-serif" font-size="18">{html.escape(title)}</text>\n'
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#555"/>\n'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#555"/>\n'
        + "\n".join(elements)
        + "\n"
        + f'<text x="{left}" y="{height-20}" font-family="sans-serif" font-size="12">{first_date.isoformat()}</text>\n'
        + f'<text x="{width-right-75}" y="{height-20}" font-family="sans-serif" font-size="12">{last_date.isoformat()}</text>\n'
        + f'<text x="8" y="{top+8}" font-family="sans-serif" font-size="12">{maximum:.4g}</text>\n'
        + f'<text x="8" y="{height-bottom}" font-family="sans-serif" font-size="12">{minimum:.4g}</text>\n'
        + f'<text x="{left}" y="{height-5}" font-family="sans-serif" font-size="11">Source: MOEX ISS. Indexed to 100 at each series start.</text>\n'
        + '</svg>\n'
    )
