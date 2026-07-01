from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path


class CalculationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PositionResult:
    instrument_id: str
    secid: str
    quantity: float
    cost_basis: float
    market_value: float
    current_nkd: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    simple_return: float | None
    annualized_return: float | None
    annualized_return_reason: str | None
    holding_days: int
    first_trade_date: date
    cash_flows: tuple[tuple[date, float], ...]


@dataclass(frozen=True)
class BenchmarkResult:
    invested: float
    ending_value: float
    return_value: float | None


@dataclass(frozen=True)
class DrawdownResult:
    current: float | None
    maximum: float | None


def load_ledger(path: Path) -> list[dict]:
    records = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or "record_type" not in record:
                raise CalculationError(f"Invalid ledger record at line {line_number}")
            records.append(record)
    except (OSError, json.JSONDecodeError) as error:
        raise CalculationError(f"Cannot read ledger {path}: {error}") from error
    return records


def _amount(record: dict, name: str) -> float:
    value = record.get(name, 0)
    if value is None:
        return 0.0
    return float(value)


def calculate_position(transactions: list[dict], latest_unit_value: float, valuation_date: date) -> PositionResult:
    relevant = [item for item in transactions if item.get("event_type") in {"buy", "sell", "coupon", "dividend", "tax"}]
    if not relevant:
        raise CalculationError("Position has no investment transactions")
    relevant.sort(key=lambda item: (item["event_date"], item.get("event_id", "")))
    instrument_id = relevant[0].get("instrument_id", "")
    secid = relevant[0].get("ticker") or relevant[0].get("secid") or instrument_id
    lots: list[list[float]] = []
    realized = 0.0
    flows: list[tuple[date, float]] = []
    first_trade = date.fromisoformat(relevant[0]["event_date"])
    for event in relevant:
        event_date = date.fromisoformat(event["event_date"])
        event_type = event["event_type"]
        if event_type == "buy":
            quantity = _amount(event, "quantity")
            if quantity <= 0:
                raise CalculationError("Buy quantity must be positive")
            basis = (
                _amount(event, "deal_amount")
                + _amount(event, "paid_nkd")
                + _amount(event, "broker_fee")
                + _amount(event, "exchange_fee")
                + _amount(event, "tax")
            )
            lots.append([quantity, basis / quantity])
            flows.append((event_date, -basis))
        elif event_type == "sell":
            remaining = _amount(event, "quantity")
            removed_basis = 0.0
            while remaining > 1e-9 and lots:
                take = min(remaining, lots[0][0])
                removed_basis += take * lots[0][1]
                lots[0][0] -= take
                remaining -= take
                if lots[0][0] <= 1e-9:
                    lots.pop(0)
            if remaining > 1e-9:
                raise CalculationError("Sell quantity exceeds open lots")
            proceeds = (
                _amount(event, "deal_amount")
                + _amount(event, "received_nkd")
                - _amount(event, "broker_fee")
                - _amount(event, "exchange_fee")
                - _amount(event, "tax")
            )
            realized += proceeds - removed_basis
            flows.append((event_date, proceeds))
        elif event_type in {"coupon", "dividend"}:
            income = _amount(event, "amount") or _amount(event, "total_cash_effect")
            realized += income
            flows.append((event_date, income))
        elif event_type == "tax":
            tax = abs(_amount(event, "amount") or _amount(event, "total_cash_effect"))
            realized -= tax
            flows.append((event_date, -tax))
    quantity = sum(lot[0] for lot in lots)
    cost_basis = sum(lot[0] * lot[1] for lot in lots)
    market_value = quantity * float(latest_unit_value)
    unrealized = market_value - cost_basis
    total_pnl = realized + unrealized
    simple_return = total_pnl / cost_basis if cost_basis else None
    holding_days = max(0, (valuation_date - first_trade).days)
    annualized = None
    reason = None
    if holding_days < 30:
        reason = "holding period is shorter than 30 days"
    else:
        terminal_flows = flows + [(valuation_date, market_value)]
        annualized = xirr(terminal_flows) if len(terminal_flows) > 2 else None
        if annualized is None and simple_return is not None and simple_return > -1:
            annualized = (1 + simple_return) ** (365 / holding_days) - 1
    return PositionResult(
        instrument_id=instrument_id,
        secid=secid,
        quantity=quantity,
        cost_basis=cost_basis,
        market_value=market_value,
        current_nkd=0.0,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        total_pnl=total_pnl,
        simple_return=simple_return,
        annualized_return=annualized,
        annualized_return_reason=reason,
        holding_days=holding_days,
        first_trade_date=first_trade,
        cash_flows=tuple(flows),
    )


def _price_on_or_after(prices: list[dict], target: date) -> float:
    for row in prices:
        if date.fromisoformat(row["date"]) >= target:
            return float(row["unit_value_rub"])
    raise CalculationError(f"No market price on or after {target.isoformat()}")


def _price_on_or_before(prices: list[dict], target: date) -> float:
    eligible = [row for row in prices if date.fromisoformat(row["date"]) <= target]
    if not eligible:
        raise CalculationError(f"No market price on or before {target.isoformat()}")
    return float(eligible[-1]["unit_value_rub"])


def benchmark_return(cash_flows: list[tuple[date, float]], prices: list[dict], valuation_date: date) -> BenchmarkResult:
    prices = sorted(prices, key=lambda row: row["date"])
    invested = 0.0
    units = 0.0
    for flow_date, amount in cash_flows:
        if amount <= 0:
            continue
        invested += amount
        units += amount / _price_on_or_after(prices, flow_date)
    ending_value = units * _price_on_or_before(prices, valuation_date)
    return BenchmarkResult(invested, ending_value, ending_value / invested - 1 if invested else None)


def calculate_drawdown(values: list[float]) -> DrawdownResult:
    if not values:
        return DrawdownResult(None, None)
    peak = float(values[0])
    maximum = 0.0
    current = 0.0
    for value in values:
        value = float(value)
        peak = max(peak, value)
        current = value / peak - 1 if peak else 0.0
        maximum = min(maximum, current)
    return DrawdownResult(current, maximum)


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    month_days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(value.day, month_days[month - 1]))


def calculate_period_return(rows: list[dict], months: int) -> float | None:
    if len(rows) < 2:
        return None
    ordered = sorted(rows, key=lambda row: row["date"])
    end_date = date.fromisoformat(ordered[-1]["date"])
    target = _subtract_months(end_date, months)
    eligible = [row for row in ordered if date.fromisoformat(row["date"]) >= target]
    if not eligible or date.fromisoformat(eligible[0]["date"]) > target + (end_date - target) / 4:
        return None
    start = float(eligible[0]["unit_value_rub"])
    end = float(ordered[-1]["unit_value_rub"])
    return end / start - 1 if start else None


def calculate_ytd_return(rows: list[dict]) -> float | None:
    if len(rows) < 2:
        return None
    ordered = sorted(rows, key=lambda row: row["date"])
    end_date = date.fromisoformat(ordered[-1]["date"])
    current_year = [row for row in ordered if date.fromisoformat(row["date"]).year == end_date.year]
    if len(current_year) < 2:
        return None
    start = float(current_year[0]["unit_value_rub"])
    end = float(current_year[-1]["unit_value_rub"])
    return end / start - 1 if start else None


def calculate_volatility(rows: list[dict]) -> float | None:
    ordered = sorted(rows, key=lambda row: row["date"])
    values = [float(row["unit_value_rub"]) for row in ordered]
    returns = [current / previous - 1 for previous, current in zip(values, values[1:]) if previous]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(252)


def xirr(cash_flows: list[tuple[date, float]]) -> float | None:
    if not cash_flows or not any(amount < 0 for _, amount in cash_flows) or not any(amount > 0 for _, amount in cash_flows):
        return None
    cash_flows = sorted(cash_flows)
    origin = cash_flows[0][0]

    def npv(rate: float) -> float:
        return sum(amount / (1 + rate) ** ((flow_date - origin).days / 365) for flow_date, amount in cash_flows)

    low, high = -0.9999, 10.0
    low_value, high_value = npv(low), npv(high)
    if low_value * high_value > 0:
        return None
    for _ in range(200):
        middle = (low + high) / 2
        value = npv(middle)
        if abs(value) < 1e-9:
            return middle
        if low_value * value <= 0:
            high = middle
        else:
            low, low_value = middle, value
    return (low + high) / 2
