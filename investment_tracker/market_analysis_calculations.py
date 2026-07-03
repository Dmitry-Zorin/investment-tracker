from __future__ import annotations

import math
import statistics
from datetime import date

from investment_tracker.dates import subtract_months
from investment_tracker.market_data import adjust_history_for_corporate_actions


WINDOW_MONTHS = {"5y": 60, "1y": 12, "3m": 3}


class MarketAnalysisError(RuntimeError):
    pass


def _rolling_mean(values: list[float], size: int, index: int) -> float | None:
    if index + 1 < size:
        return None
    return statistics.fmean(values[index + 1 - size : index + 1])


def _volatility(values: list[float]) -> float | None:
    returns = [current / previous - 1 for previous, current in zip(values, values[1:]) if previous]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(252)


def classify_range_position(value: float | None) -> str:
    if value is None:
        return "unavailable"
    if value <= 0.10:
        return "near_low"
    if value < 0.40:
        return "lower_range"
    if value <= 0.60:
        return "middle_range"
    if value < 0.90:
        return "upper_range"
    return "near_high"


def classify_drawdown(value: float | None) -> str:
    if value is None:
        return "unavailable"
    if value >= -0.01:
        return "none_or_minimal"
    if value >= -0.05:
        return "shallow"
    if value >= -0.10:
        return "moderate"
    return "deep"


def classify_ma_relation(deviation: float | None) -> str:
    if deviation is None:
        return "unavailable"
    if abs(deviation) <= 0.001:
        return "near"
    return "above" if deviation > 0 else "below"


def classify_volatility(value: float | None, full_value: float | None) -> str:
    if value is None or full_value is None or full_value == 0:
        return "unavailable"
    ratio = value / full_value
    if ratio < 0.75:
        return "subdued"
    if ratio <= 1.25:
        return "typical"
    return "elevated"


def enrich_market_rows(instrument: dict, rows: list[dict]) -> list[dict]:
    ordered = sorted(rows, key=lambda row: row["date"])
    if len({row["date"] for row in ordered}) != len(ordered):
        raise MarketAnalysisError(f"Duplicate dates for {instrument['secid']}")
    if len(ordered) < 2:
        raise MarketAnalysisError(f"At least two observations required for {instrument['secid']}")
    adjusted = adjust_history_for_corporate_actions(ordered, instrument.get("corporate_actions"))
    values = [float(row["unit_value_rub"] if instrument["type"] == "fund" else row["close"]) for row in adjusted]
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise MarketAnalysisError(f"Invalid market value for {instrument['secid']}")
    enriched = []
    peak = values[0]
    for index, (raw, normalized, value) in enumerate(zip(ordered, adjusted, values)):
        peak = max(peak, value)
        common = {
            "date": raw["date"],
            "board_id": raw["board_id"],
            "analysis_value": value,
            "daily_return": None if index == 0 else value / values[index - 1] - 1,
            "drawdown": value / peak - 1,
            "ma20": _rolling_mean(values, 20, index),
            "ma60": _rolling_mean(values, 60, index),
            "volume": raw.get("volume"),
            "turnover_rub": raw.get("value_rub"),
        }
        if instrument["type"] == "fund":
            adjusted_close = float(normalized["unit_value_rub"])
            raw_close = float(raw["close"])
            common.update(
                raw_close_rub=raw_close,
                adjusted_close_rub=adjusted_close,
                adjustment_factor=raw_close / adjusted_close,
            )
        else:
            common.update(
                clean_price_percent=float(raw["close"]),
                dirty_value_rub=float(raw["unit_value_rub"]),
                accrued_interest_rub=raw.get("accrued_interest"),
                yield_close_percent=raw.get("yield_close"),
            )
        enriched.append(common)
    return enriched


def select_window(rows: list[dict], window: str) -> tuple[list[dict], str | None, str]:
    if window == "all":
        return list(rows), None, "complete"
    if window not in WINDOW_MONTHS:
        raise MarketAnalysisError(f"Unsupported window: {window}")
    boundary = subtract_months(date.fromisoformat(rows[-1]["date"]), WINDOW_MONTHS[window])
    selected = [row for row in rows if date.fromisoformat(row["date"]) >= boundary]
    status = "complete" if date.fromisoformat(rows[0]["date"]) <= boundary else "partial"
    return selected, boundary.isoformat(), status


def _drawdown_metrics(values: list[float], dates: list[str]) -> tuple[float, float, str, str]:
    peak = values[0]
    peak_date = dates[0]
    maximum = 0.0
    maximum_peak = peak_date
    maximum_trough = peak_date
    for value, row_date in zip(values, dates):
        if value > peak:
            peak = value
            peak_date = row_date
        drawdown = value / peak - 1
        if drawdown < maximum:
            maximum = drawdown
            maximum_peak = peak_date
            maximum_trough = row_date
    return values[-1] / max(values) - 1, maximum, maximum_peak, maximum_trough


def calculate_window_metrics(rows: list[dict], window: str, full_volatility: float | None) -> dict:
    values = [float(row["analysis_value"]) for row in rows]
    dates = [row["date"] for row in rows]
    minimum = min(values)
    maximum = max(values)
    minimum_index = values.index(minimum)
    maximum_index = values.index(maximum)
    position = None if minimum == maximum else (values[-1] - minimum) / (maximum - minimum)
    current_drawdown, maximum_drawdown, peak_date, trough_date = _drawdown_metrics(values, dates)
    volatility = _volatility(values)
    ma20 = rows[-1].get("ma20")
    ma60 = rows[-1].get("ma60")
    ma20_deviation = None if not ma20 else values[-1] / ma20 - 1
    ma60_deviation = None if not ma60 else values[-1] / ma60 - 1
    turnover = [float(row["turnover_rub"]) for row in rows if row.get("turnover_rub") is not None]
    recent_turnover = turnover[-20:]
    median_20 = statistics.median(recent_turnover) if recent_turnover else None
    last_turnover = None if rows[-1].get("turnover_rub") is None else float(rows[-1]["turnover_rub"])
    result = {
        "window": window,
        "actual_start": dates[0],
        "actual_end": dates[-1],
        "observations": len(rows),
        "start_value": values[0],
        "end_value": values[-1],
        "return": values[-1] / values[0] - 1,
        "minimum": minimum,
        "minimum_date": dates[minimum_index],
        "maximum": maximum,
        "maximum_date": dates[maximum_index],
        "range_position": position,
        "range_position_bucket": classify_range_position(position),
        "current_drawdown": current_drawdown,
        "drawdown_bucket": classify_drawdown(current_drawdown),
        "maximum_drawdown": maximum_drawdown,
        "maximum_drawdown_peak_date": peak_date,
        "maximum_drawdown_trough_date": trough_date,
        "volatility": volatility,
        "volatility_context": classify_volatility(volatility, full_volatility),
        "ma20": ma20,
        "ma60": ma60,
        "ma20_deviation": ma20_deviation,
        "ma60_deviation": ma60_deviation,
        "ma20_relation": classify_ma_relation(ma20_deviation),
        "ma60_relation": classify_ma_relation(ma60_deviation),
        "average_turnover_rub": statistics.fmean(turnover) if turnover else None,
        "median_turnover_rub": statistics.median(turnover) if turnover else None,
        "last_turnover_rub": last_turnover,
        "median_20d_turnover_rub": median_20,
        "last_to_median_20d_turnover": None if not median_20 or last_turnover is None else last_turnover / median_20,
    }
    yields = [(row["date"], float(row["yield_close_percent"])) for row in rows if row.get("yield_close_percent") is not None]
    if yields:
        yield_values = [value for _, value in yields]
        result.update(
            latest_yield_percent=yields[-1][1],
            minimum_yield_percent=min(yield_values),
            minimum_yield_date=yields[yield_values.index(min(yield_values))][0],
            maximum_yield_percent=max(yield_values),
            maximum_yield_date=yields[yield_values.index(max(yield_values))][0],
            yield_change_pp=yields[-1][1] - yields[0][1],
        )
    return result


def build_instrument_analysis(instrument: dict, rows: list[dict]) -> dict:
    enriched = enrich_market_rows(instrument, rows)
    full_volatility = _volatility([float(row["analysis_value"]) for row in enriched])
    windows = {}
    for window in ("all", "5y", "1y", "3m"):
        selected, requested_start, coverage = select_window(enriched, window)
        metrics = calculate_window_metrics(selected, window, full_volatility)
        metrics["requested_start"] = requested_start
        metrics["coverage_status"] = coverage
        metrics["rows"] = selected
        windows[window] = metrics
    warnings = []
    for previous, current in zip(enriched, enriched[1:]):
        gap = (date.fromisoformat(current["date"]) - date.fromisoformat(previous["date"])).days
        if gap > 14:
            warnings.append(f"{instrument['secid']}: gap of {gap} calendar days ending {current['date']}")
    return {
        "secid": instrument["secid"],
        "name": instrument.get("name") or instrument["secid"],
        "type": instrument["type"],
        "analysis_profile": instrument.get("analysis_profile", f"generic_{instrument['type']}"),
        "corporate_actions": instrument.get("corporate_actions", []),
        "data_rows": enriched,
        "windows": windows,
        "warnings": warnings,
    }
