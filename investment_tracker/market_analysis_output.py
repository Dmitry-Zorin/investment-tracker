from __future__ import annotations

import csv
import html
import io
import json
import math
import shutil
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from investment_tracker.io_utils import format_number, sha256_file
from investment_tracker.market_analysis_calculations import build_instrument_analysis
from investment_tracker.market_data import default_analysis_profile, load_manifest, read_market_csv
from investment_tracker.workspace import WorkspacePaths


PROFILE_NOTES = {
    "money_market_fund": {"focus": ["стабильность цены", "темп накопления"], "limitations": ["ценовая зона малоинформативна без текущей ставки"]},
    "bond_fund": {"focus": ["цена", "просадка", "волатильность"], "limitations": ["нет дюрации, кривой ставок и кредитных спредов"]},
    "floating_rate_bond_fund": {"focus": ["стабильность цены", "реакция на ставку"], "limitations": ["нет ставки и лага изменения купонов"]},
    "gold_fund": {"focus": ["диапазон", "просадка", "волатильность"], "limitations": ["рублёвая цена объединяет золото и курс рубля"]},
    "government_bond": {"focus": ["чистая цена", "доходность к погашению"], "limitations": ["нет ожиданий ставки, дюрации и сравнения выпусков"]},
    "generic_fund": {"focus": ["наблюдаемая цена", "ликвидность"], "limitations": ["причинная интерпретация требует внешних данных"]},
    "generic_bond": {"focus": ["чистая цена", "доходность", "ликвидность"], "limitations": ["причинная интерпретация требует внешних данных"]},
    "gold_reference": {"focus": ["рублёвая цена золота", "диапазон", "просадка", "волатильность"], "limitations": ["рублёвая цена объединяет мировую цену золота и курс рубля"]},
}

FUND_FIELDS = ("date", "board_id", "raw_close_rub", "adjusted_close_rub", "adjustment_factor", "daily_return", "drawdown", "ma20", "ma60", "volume", "turnover_rub")
BOND_FIELDS = ("date", "board_id", "clean_price_percent", "dirty_value_rub", "accrued_interest_rub", "yield_close_percent", "daily_return", "drawdown", "ma20", "ma60", "volume", "turnover_rub")

ANALYSIS_WINDOWS = ("5y", "1y", "3m")
EXPECTED_SERIES = {
    "fund": ("turnover_rub",),
    "bond": ("turnover_rub", "yield_close_percent"),
    # The MOEX GLDRUB_TOM history endpoint carries no turnover fields, so there
    # is no expected series whose absence would count as a coverage gap.
    "reference": (),
}


class MarketAnalysisOutputError(RuntimeError):
    pass


def analysis_profile_notes(profile: str) -> dict:
    try:
        notes = PROFILE_NOTES[profile]
    except KeyError as error:
        raise MarketAnalysisOutputError(f"Unsupported analysis profile: {profile}") from error
    return {"focus": list(notes["focus"]), "limitations": list(notes["limitations"])}


def collect_missing_data(instruments: list[dict]) -> list[dict]:
    """Structured coverage gaps the package is aware of, distinct from the
    free-text ``warnings`` (which flag >14-day holes in an otherwise present
    series). Two kinds:

    - ``partial_window``: a requested window reaches further back than the
      available history, so its return/range/drawdown span a shorter period
      than the label implies.
    - ``absent_series``: a column the instrument type is expected to carry has
      no values at all (a bond with no yield history, or missing turnover),
      leaving every metric derived from it null.
    """
    records = []
    for instrument in instruments:
        secid = instrument["secid"]
        for window in ANALYSIS_WINDOWS:
            metrics = instrument["windows"].get(window)
            if metrics and metrics.get("coverage_status") == "partial":
                records.append({
                    "secid": secid,
                    "kind": "partial_window",
                    "window": window,
                    "requested_start": metrics.get("requested_start"),
                    "actual_start": metrics.get("actual_start"),
                })
        rows = instrument["data_rows"]
        for field in EXPECTED_SERIES.get(instrument["type"], ()):
            if all(row.get(field) is None for row in rows):
                records.append({"secid": secid, "kind": "absent_series", "field": field})
    return records


def build_market_analysis_model(root: Path) -> dict:
    paths = WorkspacePaths(root)
    manifest_path = paths.market_manifest
    manifest = load_manifest(manifest_path)
    instruments = []
    sources = [manifest_path]
    for item in manifest["instruments"]:
        if not item.get("enabled", True):
            continue
        path = paths.market_csv(item["secid"])
        analysis = build_instrument_analysis(item, read_market_csv(path))
        profile = item.get("analysis_profile") or default_analysis_profile(item["type"])
        notes = analysis_profile_notes(profile)
        analysis["analysis_profile"] = profile
        analysis["analysis_focus"] = notes["focus"]
        analysis["analysis_limitations"] = notes["limitations"]
        instruments.append(analysis)
        sources.append(path)
    if not instruments:
        raise MarketAnalysisOutputError("No enabled instruments in market manifest")
    latest = max(item["data_rows"][-1]["date"] for item in instruments)
    warnings = sorted({warning for item in instruments for warning in item["warnings"]})
    return {
        "schema_version": "1.0",
        "calculated_at": date.today().isoformat(),
        "latest_market_date": latest,
        "source": {"name": "MOEX ISS", "url": manifest.get("source_base_url", "https://iss.moex.com/iss")},
        "instruments": instruments,
        "warnings": warnings,
        "missing_data": collect_missing_data(instruments),
        "generated_from": [{"path": str(path.relative_to(root)), "sha256": sha256_file(path)} for path in sources],
    }


def serializable_summary(model: dict) -> dict:
    result = {key: value for key, value in model.items() if key != "instruments"}
    result["methodology"] = {
        "windows": ["all", "5y", "1y", "3m"],
        "volatility": "sample standard deviation of daily returns multiplied by sqrt(252)",
        "moving_averages": "20 and 60 trading observations with pre-window warm-up",
        "recommendations": "none; categories describe market position only",
        "missing_data": "structured coverage gaps: partial_window (history shorter than the window) and absent_series (an expected column has no values); warnings separately flag >14-day gaps in a present series",
    }
    result["instruments"] = []
    for instrument in model["instruments"]:
        copied = {key: value for key, value in instrument.items() if key not in {"data_rows", "windows"}}
        copied["windows"] = {}
        for name, window in instrument["windows"].items():
            copied["windows"][name] = {key: value for key, value in window.items() if key != "rows"}
        result["instruments"].append(copied)
    return result


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def render_analysis_markdown(model: dict) -> str:
    lines = [
        "# Оценка рыночной зоны",
        "",
        f"Дата расчёта: {model['calculated_at']}",
        f"Последняя котировка пакета: {model['latest_market_date']}",
        "",
        "Этот пакет не является инвестиционной рекомендацией и не содержит команд купить, продать или удерживать.",
        "",
    ]
    for instrument in model["instruments"]:
        name = instrument.get("name")
        heading = f"{instrument['secid']} — {name}" if name and name != instrument["secid"] else instrument["secid"]
        lines.extend([f"## {heading}", "", f"Фокус: {', '.join(instrument.get('analysis_focus', []))}.", f"Ограничения: {', '.join(instrument.get('analysis_limitations', []))}.", "", "| Окно | Период | Изменение | Положение | Просадка | Волатильность |", "| --- | --- | ---: | --- | ---: | --- |"])
        for name, window in instrument["windows"].items():
            lines.append(f"| {name} | {window['actual_start']} — {window['actual_end']} | {_pct(window['return'])} | {window['range_position_bucket']} | {_pct(window['current_drawdown'])} | {window['volatility_context']} |")
        lines.append("")
    lines.extend([
        "## Формат ответа ChatGPT",
        "",
        "Для каждого инструмента верни `zone_context`, `zone_interest` и `confidence`, затем факты за, факты против, конфликты горизонтов, ограничения и недостающие внешние данные.",
        "`zone_context`: near_low / lower_range / middle_range / upper_range / near_high / unclear.",
        "`zone_interest`: low / moderate / high / not_assessable — только приоритет дальнейшего рассмотрения, не рекомендация.",
        "`confidence`: low / medium / high — полнота данных и согласованность горизонтов, не вероятность успеха сделки.",
        "Не сравнивай инструменты между собой и не объясняй движение отсутствующими макроданными иначе чем как гипотезой для проверки.",
    ])
    return "\n".join(lines) + "\n"


def render_analytical_csv(instrument_analysis: dict) -> str:
    fields = FUND_FIELDS if instrument_analysis["type"] in {"fund", "reference"} else BOND_FIELDS
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in instrument_analysis["data_rows"]:
        writer.writerow({key: row.get(key, "") if key in {"date", "board_id"} else format_number(row.get(key)) for key in fields})
    return output.getvalue()


def _domain(values: list[float]) -> tuple[float, float]:
    low, high = min(values), max(values)
    padding = (high - low) * 0.05 if high != low else abs(low) * 0.01 or 1
    return low - padding, high + padding


def _points(rows: list[dict], key: str, left: float, top: float, width: float, height: float, domain: tuple[float, float]) -> str:
    usable = [row for row in rows if row.get(key) is not None]
    if not usable:
        return ""
    first, last = date.fromisoformat(rows[0]["date"]), date.fromisoformat(rows[-1]["date"])
    span = max(1, (last - first).days)
    low, high = domain
    return " ".join(
        f"{left + (date.fromisoformat(row['date']) - first).days / span * width:.2f},{top + (high - float(row[key])) / (high - low) * height:.2f}"
        for row in usable
    )


def render_market_chart(instrument_analysis: dict, window: str) -> str:
    metrics = instrument_analysis["windows"][window]
    rows = metrics["rows"]
    bond = instrument_analysis["type"] == "bond"
    width, height = 1000, (820 if bond else 560)
    left, plot_width = 90, 850
    price_top, price_height = 120, (300 if bond else 340)
    values = [float(row["analysis_value"]) for row in rows]
    price_domain = _domain(values)
    first_date = date.fromisoformat(rows[0]["date"])
    last_date = date.fromisoformat(rows[-1]["date"])
    day_span = max(1, (last_date - first_date).days)

    def x_for(row_date: str) -> float:
        return left + (date.fromisoformat(row_date) - first_date).days / day_span * plot_width

    def y_for(value: float, domain: tuple[float, float], top: float, panel_height: float) -> float:
        low, high = domain
        return top + (high - value) / (high - low) * panel_height

    primary = _points(rows, "analysis_value", left, price_top, plot_width, price_height, price_domain)
    title = f"{instrument_analysis['secid']}: {window}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-window="{html.escape(window)}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{left}" y="32" font-family="sans-serif" font-size="22">{html.escape(title)}</text>',
        f'<text x="{left}" y="58" font-family="sans-serif" font-size="13">{metrics["actual_start"]} — {metrics["actual_end"]}; наблюдений: {metrics["observations"]}; изменение: {_pct(metrics["return"])}; просадка: {_pct(metrics["current_drawdown"])}</text>',
        '<g id="price-panel">',
        f'<text x="{left}" y="100" font-family="sans-serif" font-size="14">{"Чистая цена, % номинала" if bond else "Скорректированная цена, ₽/пай"}</text>',
        f'<defs><clipPath id="price-clip"><rect x="{left}" y="{price_top}" width="{plot_width}" height="{price_height}"/></clipPath></defs>',
        f'<line x1="{left}" y1="{price_top + price_height}" x2="{left + plot_width}" y2="{price_top + price_height}" stroke="#777"/>',
        f'<line x1="{left}" y1="{price_top}" x2="{left}" y2="{price_top + price_height}" stroke="#777"/>',
    ]
    for index in range(5):
        fraction = index / 4
        tick_date = first_date + (last_date - first_date) * fraction
        tick_x = left + plot_width * fraction
        tick_value = price_domain[1] - (price_domain[1] - price_domain[0]) * fraction
        tick_y = price_top + price_height * fraction
        anchor = "start" if index == 0 else "end" if index == 4 else "middle"
        parts.append(f'<g class="x-tick"><line x1="{tick_x:.2f}" y1="{price_top + price_height}" x2="{tick_x:.2f}" y2="{price_top + price_height + 5}" stroke="#999"/><text x="{tick_x:.2f}" y="{price_top + price_height + 18}" text-anchor="{anchor}" font-family="sans-serif" font-size="10">{tick_date.isoformat()}</text></g>')
        parts.append(f'<g class="y-tick"><line x1="{left - 5}" y1="{tick_y:.2f}" x2="{left + plot_width}" y2="{tick_y:.2f}" stroke="#e5e7eb"/><text x="{left - 9}" y="{tick_y + 4:.2f}" text-anchor="end" font-family="sans-serif" font-size="10">{tick_value:.4g}</text></g>')
    marker_rows = {
        "minimum-marker": rows[values.index(min(values))],
        "maximum-marker": rows[values.index(max(values))],
        "current-marker": rows[-1],
    }
    for marker_class, row in marker_rows.items():
        parts.append(f'<circle class="{marker_class}" cx="{x_for(row["date"]):.2f}" cy="{y_for(float(row["analysis_value"]), price_domain, price_top, price_height):.2f}" r="4" fill="#fff" stroke="#111" stroke-width="1.5"/>')
    parts.extend([
        f'<g clip-path="url(#price-clip)"><polyline fill="none" stroke="#2563eb" stroke-width="2.5" points="{primary}"/></g>',
        f'<text x="{left}" y="{price_top + price_height + 42}" font-family="sans-serif" font-size="12">Минимум {metrics["minimum"]:.4g} ({metrics["minimum_date"]}); Максимум {metrics["maximum"]:.4g} ({metrics["maximum_date"]})</text>',
        '</g>',
    ])
    if bond:
        yield_top, yield_height = 520, 200
        yield_bottom = yield_top + yield_height
        yield_rows = [row for row in rows if row.get("yield_close_percent") is not None]
        parts.append('<g id="yield-panel">')
        parts.append(f'<text x="{left}" y="500" font-family="sans-serif" font-size="14">Доходность к погашению, %</text>')
        if yield_rows:
            yield_domain = _domain([float(row["yield_close_percent"]) for row in yield_rows])
            # Frame the panel like the price panel above: baseline, y-axis, gridlines
            # and value/date ticks, so the yield line reads as its own sub-chart
            # rather than a bare line floating below the price chart.
            parts.append(f'<line x1="{left}" y1="{yield_bottom}" x2="{left + plot_width}" y2="{yield_bottom}" stroke="#777"/>')
            parts.append(f'<line x1="{left}" y1="{yield_top}" x2="{left}" y2="{yield_bottom}" stroke="#777"/>')
            for index in range(5):
                fraction = index / 4
                tick_date = first_date + (last_date - first_date) * fraction
                tick_x = left + plot_width * fraction
                tick_value = yield_domain[1] - (yield_domain[1] - yield_domain[0]) * fraction
                tick_y = yield_top + yield_height * fraction
                anchor = "start" if index == 0 else "end" if index == 4 else "middle"
                parts.append(f'<g class="x-tick"><line x1="{tick_x:.2f}" y1="{yield_bottom}" x2="{tick_x:.2f}" y2="{yield_bottom + 5}" stroke="#999"/><text x="{tick_x:.2f}" y="{yield_bottom + 18}" text-anchor="{anchor}" font-family="sans-serif" font-size="10">{tick_date.isoformat()}</text></g>')
                parts.append(f'<g class="y-tick"><line x1="{left - 5}" y1="{tick_y:.2f}" x2="{left + plot_width}" y2="{tick_y:.2f}" stroke="#e5e7eb"/><text x="{left - 9}" y="{tick_y + 4:.2f}" text-anchor="end" font-family="sans-serif" font-size="10">{tick_value:.4g}</text></g>')
            yield_points = _points(rows, "yield_close_percent", left, yield_top, plot_width, yield_height, yield_domain)
            parts.append(f'<polyline fill="none" stroke="#7c3aed" stroke-width="2" points="{yield_points}"/>')
        else:
            parts.append(f'<text x="{left}" y="560" font-family="sans-serif" font-size="14">n/a</text>')
        parts.append('</g>')
    parts.append(f'<text x="{left}" y="{height - 18}" font-family="sans-serif" font-size="11">Источник: MOEX ISS. Последняя котировка: {metrics["actual_end"]}.</text>')
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def validate_market_analysis_model(model: dict) -> list[str]:
    errors = []
    if not model.get("instruments"):
        errors.append("no enabled instruments")
    for instrument in model.get("instruments", []):
        if len(instrument.get("data_rows", [])) < 2:
            errors.append(f"{instrument.get('secid')}: fewer than two observations")
        if set(instrument.get("windows", {})) != {"all", "5y", "1y", "3m"}:
            errors.append(f"{instrument.get('secid')}: incomplete windows")
    return errors


def export_market_analysis(root: Path, model: dict) -> Path:
    reports = WorkspacePaths(root).reports
    reports.mkdir(parents=True, exist_ok=True)
    stale = reports / "market-analysis"
    if stale.exists():
        shutil.rmtree(stale)
    archive = reports / "market-analysis.zip"
    package = stale
    (package / "data").mkdir(parents=True)
    (package / "charts").mkdir()
    (package / "analysis.md").write_text(render_analysis_markdown(model), encoding="utf-8")
    (package / "market-analysis.json").write_text(json.dumps(serializable_summary(model), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for instrument in model["instruments"]:
        secid = instrument["secid"]
        (package / "data" / f"{secid}.csv").write_text(render_analytical_csv(instrument), encoding="utf-8")
        for window in ("all", "5y", "1y", "3m"):
            (package / "charts" / f"{secid}-{window}.svg").write_text(render_market_chart(instrument, window), encoding="utf-8")
    with tempfile.NamedTemporaryFile(dir=reports, suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    zipped.write(path, Path("market-analysis") / path.relative_to(package))
        with zipfile.ZipFile(temporary) as zipped:
            json.loads(zipped.read("market-analysis/market-analysis.json"))
        temporary.replace(archive)
    finally:
        if temporary.exists():
            temporary.unlink()
    return archive
