from __future__ import annotations

import csv
import html
import hashlib
import io
import json
import math
import shutil
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

from investment_tracker.market_data import adjust_history_for_corporate_actions, load_manifest, read_market_csv
from investment_tracker.performance_calculations import (
    CalculationError,
    benchmark_return,
    calculate_drawdown,
    calculate_period_return,
    calculate_position,
    calculate_volatility,
    calculate_ytd_return,
    load_ledger,
)


class OutputError(RuntimeError):
    pass


ASSET_TYPES = {
    "bond_fund": ("fund_bond", "Фонд облигаций"),
    "floating_rate_bond_fund": ("fund_floating_rate_bond", "Фонд флоатеров"),
    "gold_fund": ("fund_gold", "Фонд золота"),
    "OFZ": ("bond_ofz", "ОФЗ"),
    "money_market_fund": ("fund_money_market", "Фонд денежного рынка"),
}

REQUIRED_POSITION_FIELDS = (
    "account_id",
    "instrument_id",
    "name",
    "asset_type",
    "quantity",
    "first_trade_date",
    "cost_basis",
    "current_value_without_nkd",
    "current_nkd",
    "current_value",
    "total_pnl",
    "return",
)


def _localize_annualized_reason(reason: str | None) -> str | None:
    if reason == "holding period is shorter than 30 days":
        return "период владения короче 30 дней"
    return reason


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}".replace(",", " ")


def _fmt_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _md_cell(value: object) -> str:
    # Neutralize characters that would break a Markdown table row: escape the
    # column separator and collapse newlines. brokerage-supplied free-form
    # strings (name, account id, asset type) are otherwise interpolated raw.
    return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _safe_float(value: object) -> float:
    return 0.0 if value is None else float(value)


def _latest_on_or_before(rows: list[dict], target: date) -> dict:
    eligible = [row for row in rows if date.fromisoformat(row["date"]) <= target]
    if not eligible:
        raise OutputError(f"No market row on or before {target.isoformat()}")
    return eligible[-1]


def _first_on_or_after(rows: list[dict], target: date) -> dict:
    for row in rows:
        if date.fromisoformat(row["date"]) >= target:
            return row
    raise OutputError(f"No market row on or after {target.isoformat()}")


def _transactions_for_position(
    ledger: list[dict], instrument_id: str, account_id: str, ticker: str
) -> list[dict]:
    transactions = []
    for record in ledger:
        if (
            record.get("record_type") == "transaction"
            and record.get("instrument_id") == instrument_id
            and record.get("account_id") == account_id
        ):
            copied = dict(record)
            copied["ticker"] = ticker
            transactions.append(copied)
    return transactions


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _warning(severity: str, code: str, message: str, affected_items: list[str] | None = None) -> dict:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "affected_items": affected_items or [],
    }


def aggregate_ticker_views(
    positions: list[dict],
    comparisons: list[dict],
    total_pnl: float,
    total_portfolio_value: float,
) -> tuple[list[dict], list[dict]]:
    pnl_groups: dict[str, dict] = {}
    for position in positions:
        group = pnl_groups.setdefault(position["ticker"], {"cost_basis": 0.0, "pnl_rub": 0.0})
        group["cost_basis"] += position["cost_basis"]
        group["pnl_rub"] += position["confirmed_pnl"]
    pnl_rows = [
        {
            "ticker": ticker,
            "pnl_rub": group["pnl_rub"],
            "pnl_pct": group["pnl_rub"] / group["cost_basis"] if group["cost_basis"] else None,
            "share_of_total_pnl_pct": group["pnl_rub"] / total_pnl if total_pnl else None,
            "portfolio_impact_pct": group["pnl_rub"] / total_portfolio_value if total_portfolio_value else None,
        }
        for ticker, group in pnl_groups.items()
    ]

    benchmark_groups: dict[str, dict] = {}
    for row in comparisons:
        group = benchmark_groups.setdefault(
            row["ticker"],
            {
                "entry_date": row["entry_date"],
                "instrument_invested_rub": 0.0,
                "instrument_ending_value_rub": 0.0,
                "benchmark_invested_rub": 0.0,
                "benchmark_ending_value_rub": 0.0,
                "methods": set(),
                "limitations": set(),
                "benchmarks": set(),
            },
        )
        group["entry_date"] = min(group["entry_date"], row["entry_date"])
        for key in (
            "instrument_invested_rub",
            "instrument_ending_value_rub",
            "benchmark_invested_rub",
            "benchmark_ending_value_rub",
        ):
            group[key] += _safe_float(row.get(key))
        group["methods"].add(row["method"])
        group["limitations"].update(row.get("limitations", []))
        group["benchmarks"].add(row["benchmark_ticker"])
    benchmark_rows = []
    for ticker, group in benchmark_groups.items():
        instrument_return = (
            group["instrument_ending_value_rub"] / group["instrument_invested_rub"] - 1
            if group["instrument_invested_rub"]
            else None
        )
        comparison_return = (
            group["benchmark_ending_value_rub"] / group["benchmark_invested_rub"] - 1
            if group["benchmark_invested_rub"]
            else None
        )
        difference = (
            instrument_return - comparison_return
            if instrument_return is not None and comparison_return is not None
            else None
        )
        if difference is None:
            interpretation = "not_available"
        elif abs(difference) <= 0.0005:
            interpretation = "approximately_equal"
        elif difference > 0:
            interpretation = "outperformed_benchmark"
        else:
            interpretation = "underperformed_benchmark"
        methods = group["methods"]
        method = "lot_based" if methods == {"lot_based"} else "not_available"
        benchmark_rows.append(
            {
                "ticker": ticker,
                "benchmark_ticker": ",".join(sorted(group["benchmarks"])),
                "method": method,
                "entry_date": group["entry_date"],
                "instrument_return_since_entry_pct": instrument_return,
                "benchmark_return_same_period_pct": comparison_return,
                "difference_pct_points": difference * 100 if difference is not None else None,
                "result_vs_benchmark_rub": (
                    group["instrument_ending_value_rub"] - group["benchmark_ending_value_rub"]
                    if difference is not None
                    else None
                ),
                "interpretation": interpretation,
                "limitations": sorted(group["limitations"]),
            }
        )
    return pnl_rows, benchmark_rows


def build_report_model(root: Path) -> dict:
    manifest = load_manifest(root / "data/market/manifest.json")
    try:
        brokerage = json.loads((root / "brokerage-current.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OutputError(f"Cannot read brokerage-current.json: {error}") from error
    ledger = load_ledger(root / "brokerage-ledger.jsonl")
    by_id = {item["instrument_id"]: item for item in manifest["instruments"] if item.get("enabled")}
    histories = {}
    for item in by_id.values():
        raw_rows = read_market_csv(root / "data/market" / f"{item['secid']}.csv")
        histories[item["secid"]] = adjust_history_for_corporate_actions(
            raw_rows, item.get("corporate_actions")
        )
    if not histories:
        raise OutputError("No enabled instruments in market manifest")
    if any(not rows for rows in histories.values()):
        raise OutputError("One or more enabled instruments have empty market history")
    latest_market_date = max(rows[-1]["date"] for rows in histories.values())
    valuation_date = date.fromisoformat(latest_market_date)
    warnings = [
        _warning(
            "warning",
            warning.get("warning_id", "brokerage_warning").replace("-", "_"),
            warning.get("message", str(warning)),
        )
        for warning in brokerage.get("warnings", [])
    ]
    missing_data: list[str] = []
    positions = []
    benchmark_comparison = []
    instrument_period_returns = []
    commissions_complete = True
    aci_complete = True
    benchmarks = sorted(
        {item["benchmark"] for item in by_id.values() if item.get("benchmark")}
    )
    for snapshot in brokerage.get("positions", []):
        missing = [field for field in REQUIRED_POSITION_FIELDS if field not in snapshot]
        if missing:
            raise OutputError(
                f"Brokerage position {snapshot.get('instrument_id', '?')} is missing required "
                f"fields: {', '.join(missing)}"
            )
        try:
            snapshot_quantity = float(snapshot["quantity"])
        except (TypeError, ValueError) as error:
            raise OutputError(
                f"Brokerage position {snapshot['instrument_id']} has non-numeric quantity: "
                f"{snapshot['quantity']!r}"
            ) from error
        if snapshot_quantity == 0:
            raise OutputError(
                f"Brokerage position {snapshot['instrument_id']} has zero quantity"
            )
        instrument = by_id.get(snapshot["instrument_id"])
        if not instrument:
            warnings.append(
                _warning(
                    "error",
                    "missing_market_instrument",
                    f"No enabled market instrument for {snapshot['instrument_id']}",
                    [snapshot["instrument_id"]],
                )
            )
            continue
        rows = histories[instrument["secid"]]
        benchmark_ticker = instrument.get("benchmark")
        if not benchmark_ticker:
            raise OutputError(f"No benchmark configured for {instrument['secid']}")
        benchmark_rows = histories.get(benchmark_ticker, [])
        last = _latest_on_or_before(rows, valuation_date)
        transactions = _transactions_for_position(
            ledger, snapshot["instrument_id"], snapshot["account_id"], instrument["secid"]
        )
        try:
            result = calculate_position(transactions, float(last["unit_value_rub"]), valuation_date)
        except CalculationError as error:
            warnings.append(_warning("error", "position_calculation_failed", str(error), [instrument["secid"]]))
            continue
        first = _first_on_or_after(rows, result.first_trade_date)
        rows_since_entry = [row for row in rows if row["date"] >= first["date"] and row["date"] <= last["date"]]
        public_return = float(last["unit_value_rub"]) / float(first["unit_value_rub"]) - 1
        # Only capital actually deployed into the market (buy cost bases) is
        # replayed into the benchmark. Taxes and other frictions are excluded:
        # they already reduce realized PnL and are not benchmark contributions,
        # so benchmark.invested stays equal to result.total_invested.
        contribution_flows = list(result.contribution_flows)
        benchmark = None
        try:
            benchmark = benchmark_return(contribution_flows, benchmark_rows, valuation_date)
        except CalculationError as error:
            warnings.append(_warning("warning", "benchmark_not_available", str(error), [instrument["secid"]]))
        drawdown = calculate_drawdown([float(row["unit_value_rub"]) for row in rows_since_entry])
        period_returns = {f"{months}m": calculate_period_return(rows, months) for months in (1, 3, 6, 12)}
        period_returns["ytd"] = calculate_ytd_return(rows)
        for period, value in period_returns.items():
            if value is None:
                missing_data.append(f"{instrument['secid']}: insufficient history for {period} return")
        confirmed_value = float(snapshot["current_value"])
        confirmed_pnl = float(snapshot["total_pnl"])
        confirmed_return = float(snapshot["return"])
        preliminary_value = result.market_value
        source_asset_type = snapshot["asset_type"]
        normalized_asset_type, asset_type_label = ASSET_TYPES.get(
            source_asset_type,
            (f"other_{str(source_asset_type).lower()}", str(source_asset_type)),
        )
        buy_events = [event for event in transactions if event.get("event_type") == "buy"]
        fee_fields_available = all(
            "broker_fee" in event and "exchange_fee" in event for event in transactions
        )
        commissions_complete = commissions_complete and fee_fields_available
        commissions = (
            sum(_safe_float(event.get("broker_fee")) + _safe_float(event.get("exchange_fee")) for event in transactions)
            if fee_fields_available
            else None
        )
        is_bond = instrument["type"] == "bond"
        if is_bond and "current_nkd" not in snapshot:
            aci_complete = False
        accrued_coupon_income = _safe_float(snapshot.get("current_nkd")) if is_bond and "current_nkd" in snapshot else None
        last_entry_date = max((event["event_date"] for event in buy_events), default=snapshot["first_trade_date"])
        benchmark_return_value = benchmark.return_value if benchmark else None
        difference = (
            result.simple_return - benchmark_return_value
            if result.simple_return is not None and benchmark_return_value is not None
            else None
        )
        if difference is None:
            interpretation = "not_available"
        elif abs(difference) <= 0.0005:
            interpretation = "approximately_equal"
        elif difference > 0:
            interpretation = "outperformed_benchmark"
        else:
            interpretation = "underperformed_benchmark"
        benchmark_row = {
            "ticker": instrument["secid"],
            "benchmark_ticker": benchmark_ticker,
            "method": "lot_based" if benchmark else "not_available",
            "entry_date": snapshot["first_trade_date"],
            "instrument_return_since_entry_pct": result.simple_return,
            "benchmark_return_same_period_pct": benchmark_return_value,
            "difference_pct_points": difference * 100 if difference is not None else None,
            "result_vs_benchmark_rub": (
                result.total_invested + result.total_pnl - benchmark.ending_value if benchmark else None
            ),
            "interpretation": interpretation,
            "limitations": [] if benchmark else ["Insufficient market history for exact lot-based comparison."],
            "instrument_invested_rub": result.total_invested,
            "instrument_ending_value_rub": result.total_invested + result.total_pnl,
            "benchmark_invested_rub": benchmark.invested if benchmark else None,
            "benchmark_ending_value_rub": benchmark.ending_value if benchmark else None,
        }
        benchmark_comparison.append(benchmark_row)
        instrument_period_returns.append(
            {
                "ticker": instrument["secid"],
                "name": snapshot["name"],
                "asset_class": normalized_asset_type,
                "returns_pct": period_returns,
                "last_quote_date": last["date"],
                "source": "MOEX",
            }
        )
        positions.append(
            {
                "ticker": instrument["secid"],
                "instrument_id": snapshot["instrument_id"],
                "name": snapshot["name"],
                "account_id": snapshot["account_id"],
                "account_alias": snapshot["account_id"],
                "asset_type": normalized_asset_type,
                "asset_class": normalized_asset_type,
                "asset_type_label": asset_type_label,
                "source_asset_type": source_asset_type,
                "quantity": float(snapshot["quantity"]),
                "avg_entry_price_rub": float(snapshot["cost_basis"]) / float(snapshot["quantity"]),
                "first_trade_date": snapshot["first_trade_date"],
                "first_entry_date": snapshot["first_trade_date"],
                "last_entry_date": last_entry_date,
                "holding_days": result.holding_days,
                "cost_basis": float(snapshot["cost_basis"]),
                "confirmed_value": confirmed_value,
                "confirmed_value_components": {
                    "clean_value": float(snapshot["current_value_without_nkd"]),
                    "accrued_interest": float(snapshot["current_nkd"]),
                    "total_value": confirmed_value,
                },
                "confirmed_pnl": confirmed_pnl,
                "confirmed_return": confirmed_return,
                "preliminary_value": preliminary_value,
                "preliminary_pnl": result.total_pnl,
                "preliminary_return": result.simple_return,
                "estimated_market_value_rub": preliminary_value,
                "estimated_market_pnl_rub": result.total_pnl,
                "estimated_market_pnl_pct": result.simple_return,
                "public_return_since_entry": public_return,
                "benchmark_return": benchmark_return_value,
                "current_drawdown": drawdown.current,
                "max_drawdown": drawdown.maximum,
                "volatility": calculate_volatility(rows_since_entry),
                "annualized_return": result.annualized_return,
                "annualized_return_reason": _localize_annualized_reason(result.annualized_return_reason),
                "period_returns": period_returns,
                "market_date": last["date"],
                "commissions_rub": commissions,
                "accrued_coupon_income_rub": accrued_coupon_income,
                "source": "brokerage_snapshot",
            }
        )
    calculation_date = date.today()
    age = (calculation_date - date.fromisoformat(latest_market_date)).days
    if age > 30:
        warnings.append(_warning("warning", "stale_market_quote", f"Market data are stale by {age} days."))
    elif age > 0:
        warnings.append(
            _warning(
                "info",
                "market_quote_precedes_calculation",
                f"The last public market quote precedes the calculation date by {age} day(s).",
            )
        )
    brokerage_age = (calculation_date - date.fromisoformat(brokerage["as_of"])).days
    if brokerage_age > 30:
        warnings.append(
            _warning(
                "warning",
                "stale_brokerage_snapshot",
                "Brokerage snapshot is older than 30 days.",
            )
        )
    if date.fromisoformat(latest_market_date) < date.fromisoformat(brokerage["as_of"]):
        warnings.append(
            _warning(
                "warning",
                "market_quote_older_than_brokerage_snapshot",
                "The last market quote is older than the brokerage snapshot.",
            )
        )
    if not commissions_complete:
        warnings.append(
            _warning("warning", "partial_commissions", "Commission data are only partially available.")
        )
    if not aci_complete:
        warnings.append(
            _warning("warning", "partial_accrued_coupon_income", "Accrued coupon income is only partially available.")
        )
    for instrument in manifest["instruments"]:
        warnings.extend(
            _warning("warning", "market_data_warning", warning, [instrument["secid"]])
            for warning in instrument.get("warnings", [])
        )
    source_paths = [
        root / "brokerage-current.json",
        root / "brokerage-ledger.jsonl",
        root / "data/market/manifest.json",
    ]
    source_paths.extend(root / "data/market" / f"{item['secid']}.csv" for item in by_id.values())
    roles = {
        "brokerage-current.json": "brokerage_snapshot",
        "brokerage-ledger.jsonl": "brokerage_ledger",
        "data/market/manifest.json": "market_manifest",
    }
    generated_from = [
        {
            "path": str(path.relative_to(root)),
            "sha256": _sha256(path),
            "role": roles.get(str(path.relative_to(root)), "market_data"),
        }
        for path in source_paths
    ]
    totals = brokerage.get("totals", {})
    total_cost_basis = _safe_float(totals.get("open_position_cost_basis"))
    confirmed_value = _safe_float(totals.get("securities_total"))
    confirmed_pnl = _safe_float(totals.get("total_pnl"))
    cash = _safe_float(totals.get("cash"))
    estimated_value = sum(position["preliminary_value"] for position in positions)
    estimated_pnl = sum(position["preliminary_pnl"] for position in positions)
    portfolio = {
        "total_cost_basis_rub": total_cost_basis,
        "confirmed_value_rub": confirmed_value,
        "estimated_market_value_rub": estimated_value,
        "confirmed_pnl_rub": confirmed_pnl,
        "confirmed_pnl_pct": confirmed_pnl / total_cost_basis if total_cost_basis else None,
        "estimated_market_pnl_rub": estimated_pnl,
        "estimated_market_pnl_pct": estimated_pnl / total_cost_basis if total_cost_basis else None,
        "cash_rub": cash,
        "positions_count": len(positions),
    }
    pnl_contribution, benchmark_comparison = aggregate_ticker_views(
        positions,
        benchmark_comparison,
        total_pnl=confirmed_pnl,
        total_portfolio_value=confirmed_value + cash,
    )
    unique_period_returns = {
        row["ticker"]: row for row in instrument_period_returns
    }
    return {
        "calculated_at": calculation_date.isoformat(),
        "brokerage_snapshot_date": brokerage["as_of"],
        "latest_market_date": latest_market_date,
        "benchmarks": benchmarks,
        "data_status": {
            "brokerage_snapshot": "confirmed",
            "market_prices": "estimated_from_moex",
            "commissions": "confirmed" if commissions_complete else "confirmed_or_partial",
            "accrued_coupon_income": "confirmed" if aci_complete else "confirmed_or_partial",
            "taxes": "not_calculated",
            "bank_deposit_benchmark": "not_available",
            "intraday_spreads": "not_included",
            "entry_exit_timing": "out_of_scope",
        },
        "portfolio": portfolio,
        "positions": positions,
        "pnl_contribution": pnl_contribution,
        "benchmark_comparison": benchmark_comparison,
        "instrument_period_returns": list(unique_period_returns.values()),
        "warnings": sorted(warnings, key=lambda item: (item["code"], item["message"])),
        "missing_data": sorted(set(missing_data)),
        "histories": histories,
        "market_units": {
            item["secid"]: (
                "dirty bond price, RUB per bond" if item["type"] == "bond" else "RUB per unit"
            )
            for item in by_id.values()
        },
        "corporate_actions": [
            {"secid": item["secid"], **action}
            for item in by_id.values()
            for action in item.get("corporate_actions", [])
        ],
        "generated_from": generated_from,
    }


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


def build_market_summary(model: dict) -> dict:
    positions = []
    for position in model["positions"]:
        positions.append(
            {
                "account_alias": position.get("account_alias", position["account_id"]),
                "ticker": position["ticker"],
                "name": position["name"],
                "asset_class": position.get("asset_class", position["asset_type"]),
                "quantity": position["quantity"],
                "avg_entry_price_rub": position.get("avg_entry_price_rub"),
                "cost_basis_rub": position["cost_basis"],
                "confirmed_value_rub": position["confirmed_value"],
                "estimated_market_value_rub": position["preliminary_value"],
                "confirmed_pnl_rub": position["confirmed_pnl"],
                "confirmed_pnl_pct": position["confirmed_return"],
                "estimated_market_pnl_rub": position["preliminary_pnl"],
                "estimated_market_pnl_pct": position["preliminary_return"],
                "first_entry_date": position.get("first_entry_date", position.get("first_trade_date")),
                "last_entry_date": position.get("last_entry_date"),
                "commissions_rub": position.get("commissions_rub"),
                "accrued_coupon_income_rub": position.get("accrued_coupon_income_rub"),
                "source": position.get("source", "brokerage_snapshot"),
            }
        )
    return {
        "schema_version": "1.0",
        "metadata": {
            "purpose": "portfolio_performance_and_benchmark_comparison",
            "not_for": [
                "entry_exit_timing",
                "technical_analysis",
                "market_zone_analysis",
                "buy_sell_signals",
            ],
            "calculated_at": model["calculated_at"],
            "brokerage_snapshot_date": model["brokerage_snapshot_date"],
            "last_market_quote_date": model["latest_market_date"],
            "currency": "RUB",
            "benchmarks": model.get("benchmarks", []),
        },
        "data_status": model["data_status"],
        "portfolio": model["portfolio"],
        "positions": positions,
        "pnl_contribution": model.get("pnl_contribution", []),
        "benchmark_comparison": model.get("benchmark_comparison", []),
        "instrument_period_returns": model.get("instrument_period_returns", []),
        "warnings": model["warnings"],
        "generated_from": model.get("generated_from", []),
        "corporate_actions": model.get("corporate_actions", []),
    }


def render_line_chart(
    title: str,
    rows: list[dict],
    value_key: str,
    comparison_rows: list[dict] | None = None,
    unit_label: str | None = None,
) -> str:
    if len(rows) < 2:
        raise OutputError(f"At least two rows are required for chart {title}")
    width, height = 900, 450
    left, right, top, bottom = 70, 25, 45, 75
    values = [float(row[value_key]) for row in rows]
    if not all(math.isfinite(value) for value in values):
        raise OutputError(f"Non-finite chart value for {title}")
    minimum, maximum = min(values), max(values)
    if minimum == maximum:
        minimum -= 1
        maximum += 1

    def points(series: list[dict], key: str) -> str:
        series_values = [float(row[key]) for row in series]
        result = []
        for index, value in enumerate(series_values):
            x = left + index * (width - left - right) / max(1, len(series_values) - 1)
            y = top + (maximum - value) * (height - top - bottom) / (maximum - minimum)
            result.append(f"{x:.2f},{y:.2f}")
        return " ".join(result)

    escaped = html.escape(title)
    start, end = html.escape(rows[0]["date"]), html.escape(rows[-1]["date"])
    comparison = ""
    if comparison_rows:
        comparison = f'<polyline fill="none" stroke="#d97706" stroke-width="2" points="{points(comparison_rows, value_key)}"/>'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        '<rect width="100%" height="100%" fill="#ffffff"/>\n'
        f'<text x="{left}" y="28" font-family="sans-serif" font-size="18">{escaped}</text>\n'
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#555"/>\n'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#555"/>\n'
        f'<polyline fill="none" stroke="#2563eb" stroke-width="2" points="{points(rows, value_key)}"/>\n'
        f'{comparison}\n'
        f'<text x="{left}" y="{height-20}" font-family="sans-serif" font-size="12">{start}</text>\n'
        f'<text x="{width-right-75}" y="{height-20}" font-family="sans-serif" font-size="12">{end}</text>\n'
        f'<text x="8" y="{top+8}" font-family="sans-serif" font-size="12">{maximum:.4g}</text>\n'
        f'<text x="8" y="{height-bottom}" font-family="sans-serif" font-size="12">{minimum:.4g}</text>\n'
        f'<text x="{left}" y="{height-5}" font-family="sans-serif" font-size="11">Source: MOEX ISS. Last quote: {end}. Values: {html.escape(unit_label or value_key)}.</text>\n'
        '</svg>\n'
    )


def render_multi_line_chart(title: str, series: dict[str, list[dict]], value_key: str) -> str:
    usable = {label: rows for label, rows in series.items() if len(rows) >= 2}
    if not usable:
        raise OutputError(f"At least one two-point series is required for chart {title}")
    width, height = 900, 450
    left, right, top, bottom = 70, 140, 45, 75
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
    paths = []
    legends = []
    for index, (label, rows) in enumerate(sorted(usable.items())):
        color = colors[index % len(colors)]
        paths.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points(rows)}"/>')
        legend_y = top + index * 22
        legends.append(f'<line x1="{width-right+15}" y1="{legend_y}" x2="{width-right+35}" y2="{legend_y}" stroke="{color}" stroke-width="2"/>')
        legends.append(f'<text x="{width-right+42}" y="{legend_y+4}" font-family="sans-serif" font-size="12">{html.escape(label)}</text>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        '<rect width="100%" height="100%" fill="#ffffff"/>\n'
        f'<text x="{left}" y="28" font-family="sans-serif" font-size="18">{html.escape(title)}</text>\n'
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#555"/>\n'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#555"/>\n'
        + "\n".join(paths + legends)
        + "\n"
        + f'<text x="{left}" y="{height-20}" font-family="sans-serif" font-size="12">{first_date.isoformat()}</text>\n'
        + f'<text x="{width-right-75}" y="{height-20}" font-family="sans-serif" font-size="12">{last_date.isoformat()}</text>\n'
        + f'<text x="8" y="{top+8}" font-family="sans-serif" font-size="12">{maximum:.4g}</text>\n'
        + f'<text x="8" y="{height-bottom}" font-family="sans-serif" font-size="12">{minimum:.4g}</text>\n'
        + f'<text x="{left}" y="{height-5}" font-family="sans-serif" font-size="11">Source: MOEX ISS. Last quote: {last_date.isoformat()}. Values: normalized index.</text>\n'
        + '</svg>\n'
    )


def render_bar_chart(title: str, categories: list[tuple[str, float]], unit: str) -> str:
    if not categories:
        raise OutputError(f"At least one category is required for chart {title}")
    width, height = 900, 450
    left, right, top, bottom = 70, 25, 45, 95
    values = [float(value) for _, value in categories]
    minimum, maximum = min(0.0, min(values)), max(0.0, max(values))
    if minimum == maximum:
        maximum = minimum + 1
    plot_height = height - top - bottom

    def y(value: float) -> float:
        return top + (maximum - value) * plot_height / (maximum - minimum)

    baseline = y(0)
    slot = (width - left - right) / len(categories)
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
        elements.append(
            f'<text x="{center:.2f}" y="{height-bottom+25}" text-anchor="middle" font-family="sans-serif" font-size="12">{html.escape(label)}</text>'
        )
        elements.append(
            f'<text x="{center:.2f}" y="{bar_y-6:.2f}" text-anchor="middle" font-family="sans-serif" font-size="12">{value:.2f}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        '<rect width="100%" height="100%" fill="#ffffff"/>\n'
        f'<text x="{left}" y="28" font-family="sans-serif" font-size="18">{html.escape(title)}</text>\n'
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


def _normalized_rows(rows: list[dict]) -> list[dict]:
    first = float(rows[0]["unit_value_rub"])
    return [{"date": row["date"], "normalized": float(row["unit_value_rub"]) / first * 100} for row in rows]


PORTFOLIO_DATA_FILES = (
    "portfolio-summary.csv",
    "positions.csv",
    "benchmark-comparison.csv",
    "instrument-period-returns.csv",
    "pnl-contribution.csv",
)

PORTFOLIO_CHART_FILES = (
    "portfolio-composition.svg",
    "positions-pnl.svg",
    "positions-vs-benchmark.svg",
    "instruments-period-returns.svg",
    "pnl-contribution.svg",
)

CSV_SCHEMAS = {
    "portfolio-summary.csv": ["metric", "value", "currency", "source"],
    "positions.csv": [
        "account_alias", "ticker", "name", "asset_class", "quantity", "avg_entry_price_rub",
        "cost_basis_rub", "confirmed_value_rub", "estimated_market_value_rub", "confirmed_pnl_rub",
        "confirmed_pnl_pct", "estimated_market_pnl_rub", "estimated_market_pnl_pct", "first_entry_date",
        "last_entry_date", "commissions_rub", "accrued_coupon_income_rub", "source",
    ],
    "benchmark-comparison.csv": [
        "ticker", "benchmark_ticker", "method", "entry_date", "instrument_return_since_entry_pct",
        "benchmark_return_same_period_pct", "difference_pct_points", "result_vs_benchmark_rub",
        "interpretation", "limitations",
    ],
    "instrument-period-returns.csv": [
        "ticker", "name", "asset_class", "return_1m_pct", "return_3m_pct", "return_6m_pct",
        "return_12m_pct", "return_ytd_pct", "last_quote_date", "source",
    ],
    "pnl-contribution.csv": [
        "ticker", "pnl_rub", "pnl_pct", "share_of_total_pnl_pct", "portfolio_impact_pct"
    ],
}


def validate_portfolio_outputs(root: Path) -> list[str]:
    reports = root / "reports"
    package = reports / "chatgpt-export"
    errors: list[str] = []
    required = [
        package / "performance.md",
        package / "market-summary.json",
        *(package / "data" / name for name in PORTFOLIO_DATA_FILES),
        *(package / "charts" / name for name in PORTFOLIO_CHART_FILES),
    ]
    for path in required:
        if not path.is_file():
            errors.append(f"missing generated file: {path.relative_to(root)}")
    if errors:
        return errors
    try:
        summary = json.loads((package / "market-summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"invalid market-summary.json: {error}"]
    required_roots = {
        "schema_version", "metadata", "data_status", "portfolio", "positions", "pnl_contribution",
        "benchmark_comparison", "instrument_period_returns", "warnings", "generated_from",
    }
    for key in sorted(required_roots - summary.keys()):
        errors.append(f"missing summary field: {key}")
    metadata = summary.get("metadata", {})
    if metadata.get("purpose") != "portfolio_performance_and_benchmark_comparison":
        errors.append("missing metadata purpose")
    required_not_for = {
        "entry_exit_timing", "technical_analysis", "market_zone_analysis", "buy_sell_signals"
    }
    if not required_not_for.issubset(set(metadata.get("not_for", []))):
        errors.append("metadata not_for is incomplete")
    prohibited_keys = {
        "ma20", "ma60", "rsi", "macd", "support", "resistance", "entry_level", "exit_level",
        "recommendation",
    }

    def scan_keys(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key.lower() in prohibited_keys:
                    errors.append(f"prohibited summary field: {key}")
                scan_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                scan_keys(nested)
        elif isinstance(value, str) and value.lower() in {"buy", "sell", "hold"}:
            errors.append(f"prohibited trading action: {value.lower()}")

    scan_keys(summary)
    warning_codes = {item.get("code") for item in summary.get("warnings", []) if isinstance(item, dict)}
    missing_quote_items = {
        affected
        for item in summary.get("warnings", [])
        if isinstance(item, dict) and item.get("code") == "missing_market_quote"
        for affected in item.get("affected_items", [])
    }
    for position in summary.get("positions", []):
        if position.get("estimated_market_value_rub") is None and position.get("ticker") not in missing_quote_items:
            errors.append(f"missing market quote warning: {position.get('ticker')}")
    try:
        calculated_at = date.fromisoformat(metadata["calculated_at"])
        snapshot_at = date.fromisoformat(metadata["brokerage_snapshot_date"])
        quote_at = date.fromisoformat(metadata["last_market_quote_date"])
    except (KeyError, TypeError, ValueError):
        errors.append("invalid metadata dates")
    else:
        if (calculated_at - snapshot_at).days > 30 and "stale_brokerage_snapshot" not in warning_codes:
            errors.append("missing stale brokerage snapshot warning")
        if quote_at < snapshot_at and "market_quote_older_than_brokerage_snapshot" not in warning_codes:
            errors.append("missing old market quote warning")
    if summary.get("data_status", {}).get("commissions") == "confirmed_or_partial" and "partial_commissions" not in warning_codes:
        errors.append("missing partial commission warning")
    for row in summary.get("benchmark_comparison", []):
        if row.get("method") not in {"lot_based", "not_available"} and not row.get("limitations"):
            errors.append(f"approximate benchmark row lacks limitation: {row.get('ticker')}")
    for source in summary.get("generated_from", []):
        path = root / source.get("path", "")
        digest = source.get("sha256")
        if not path.is_file():
            errors.append(f"missing generated_from source: {source.get('path')}")
        elif not isinstance(digest, str) or digest != _sha256(path):
            errors.append(f"generated_from hash mismatch: {source.get('path')}")
        if not source.get("role"):
            errors.append(f"generated_from role missing: {source.get('path')}")
    for name, expected_header in CSV_SCHEMAS.items():
        path = package / "data" / name
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
        except OSError as error:
            errors.append(f"cannot read CSV {name}: {error}")
            continue
        if not rows or rows[0] != expected_header:
            errors.append(f"invalid CSV header: {name}")
        elif summary.get("positions") and len(rows) < 2:
            errors.append(f"CSV has no data rows: {name}")
    for name in PORTFOLIO_CHART_FILES:
        try:
            content = (package / "charts" / name).read_text(encoding="utf-8")
        except OSError as error:
            errors.append(f"cannot read SVG {name}: {error}")
            continue
        if "<svg" not in content:
            errors.append(f"invalid SVG: {name}")
    report = (package / "performance.md").read_text(encoding="utf-8")
    for marker in (
        "## 1. Metadata", "## 2. Purpose and scope", "Confirmed by brokerage snapshot",
        "Estimated from MOEX/public market data", "## 11. Data limitations",
    ):
        if marker not in report:
            errors.append(f"performance.md missing: {marker}")
    archive = reports / "chatgpt-export.zip"
    if archive.exists():
        expected_names = {
            "chatgpt-export/performance.md",
            "chatgpt-export/market-summary.json",
            *(f"chatgpt-export/data/{name}" for name in PORTFOLIO_DATA_FILES),
            *(f"chatgpt-export/charts/{name}" for name in PORTFOLIO_CHART_FILES),
        }
        try:
            with zipfile.ZipFile(archive) as zipped:
                names = {name for name in zipped.namelist() if not name.endswith("/")}
        except (OSError, zipfile.BadZipFile) as error:
            errors.append(f"invalid chatgpt-export.zip: {error}")
        else:
            if names != expected_names:
                errors.append("chatgpt-export.zip has unexpected contents")
            directory_names = {
                f"chatgpt-export/{path.relative_to(package)}"
                for path in package.rglob("*")
                if path.is_file()
            }
            if names != directory_names:
                errors.append("chatgpt-export.zip does not match report directory")
    return errors


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write(path, buffer.getvalue())


def _write_portfolio_csvs(package: Path, model: dict) -> None:
    data = package / "data"
    portfolio = model["portfolio"]
    _write_csv(
        data / "portfolio-summary.csv",
        ["metric", "value", "currency", "source"],
        [
            {"metric": "total_cost_basis", "value": portfolio["total_cost_basis_rub"], "currency": "RUB", "source": "brokerage_snapshot"},
            {"metric": "confirmed_value", "value": portfolio["confirmed_value_rub"], "currency": "RUB", "source": "brokerage_snapshot"},
            {"metric": "estimated_market_value", "value": portfolio["estimated_market_value_rub"], "currency": "RUB", "source": "moex"},
            {"metric": "confirmed_pnl_rub", "value": portfolio["confirmed_pnl_rub"], "currency": "RUB", "source": "brokerage_snapshot"},
            {"metric": "confirmed_pnl_pct", "value": portfolio["confirmed_pnl_pct"], "currency": "percent", "source": "brokerage_snapshot"},
            {"metric": "cash_rub", "value": portfolio["cash_rub"], "currency": "RUB", "source": "brokerage_snapshot"},
        ],
    )
    summary_positions = build_market_summary(model)["positions"]
    _write_csv(
        data / "positions.csv",
        [
            "account_alias", "ticker", "name", "asset_class", "quantity", "avg_entry_price_rub",
            "cost_basis_rub", "confirmed_value_rub", "estimated_market_value_rub", "confirmed_pnl_rub",
            "confirmed_pnl_pct", "estimated_market_pnl_rub", "estimated_market_pnl_pct", "first_entry_date",
            "last_entry_date", "commissions_rub", "accrued_coupon_income_rub", "source",
        ],
        summary_positions,
    )
    _write_csv(
        data / "benchmark-comparison.csv",
        [
            "ticker", "benchmark_ticker", "method", "entry_date", "instrument_return_since_entry_pct",
            "benchmark_return_same_period_pct", "difference_pct_points", "result_vs_benchmark_rub",
            "interpretation", "limitations",
        ],
        [
            {**row, "limitations": "; ".join(row.get("limitations", []))}
            for row in model.get("benchmark_comparison", [])
        ],
    )
    _write_csv(
        data / "instrument-period-returns.csv",
        [
            "ticker", "name", "asset_class", "return_1m_pct", "return_3m_pct", "return_6m_pct",
            "return_12m_pct", "return_ytd_pct", "last_quote_date", "source",
        ],
        [
            {
                "ticker": row["ticker"],
                "name": row["name"],
                "asset_class": row["asset_class"],
                "return_1m_pct": row["returns_pct"]["1m"],
                "return_3m_pct": row["returns_pct"]["3m"],
                "return_6m_pct": row["returns_pct"]["6m"],
                "return_12m_pct": row["returns_pct"]["12m"],
                "return_ytd_pct": row["returns_pct"]["ytd"],
                "last_quote_date": row["last_quote_date"],
                "source": row["source"],
            }
            for row in model.get("instrument_period_returns", [])
        ],
    )
    _write_csv(
        data / "pnl-contribution.csv",
        ["ticker", "pnl_rub", "pnl_pct", "share_of_total_pnl_pct", "portfolio_impact_pct"],
        model.get("pnl_contribution", []),
    )


def _write_portfolio_charts(package: Path, model: dict) -> None:
    charts = package / "charts"
    composition_by_ticker: dict[str, float] = {}
    for position in model["positions"]:
        composition_by_ticker[position["ticker"]] = (
            composition_by_ticker.get(position["ticker"], 0.0) + position["confirmed_value"]
        )
    composition = list(composition_by_ticker.items())
    if model["portfolio"]["cash_rub"]:
        composition.append(("CASH", model["portfolio"]["cash_rub"]))
    if not composition:
        composition.append(("not available", 0))
    _atomic_write(
        charts / "portfolio-composition.svg",
        render_bar_chart(
            f"Portfolio composition at {model['brokerage_snapshot_date']}",
            composition,
            "RUB; source: brokerage snapshot",
        ),
    )
    _atomic_write(
        charts / "positions-pnl.svg",
        render_bar_chart(
            "Confirmed PnL by position",
            [(row["ticker"], row["pnl_rub"]) for row in model.get("pnl_contribution", [])]
            or [("not available", 0)],
            "RUB; confirmed brokerage snapshot",
        ),
    )
    benchmark_values = [
        (
            f"{row['ticker']} ({_fmt_money(row['result_vs_benchmark_rub'])} RUB)",
            row["difference_pct_points"],
        )
        for row in model.get("benchmark_comparison", [])
        if row["difference_pct_points"] is not None
    ]
    _atomic_write(
        charts / "positions-vs-benchmark.svg",
        render_bar_chart(
            "Positions vs configured benchmark (lot-based; not a trading signal)",
            benchmark_values or [("not available", 0)],
            "percentage points; sources: brokerage entries + MOEX",
        ),
    )
    _atomic_write(
        charts / "instruments-period-returns.svg",
        render_period_returns_chart(model.get("instrument_period_returns", [])),
    )
    contribution_values = [
        (
            f"{row['ticker']} ({_fmt_percent(row['share_of_total_pnl_pct'])})",
            row["pnl_rub"],
        )
        for row in model.get("pnl_contribution", [])
    ]
    if not contribution_values:
        contribution_values.append(("not available", 0))
    _atomic_write(
        charts / "pnl-contribution.svg",
        render_bar_chart(
            "Confirmed PnL contribution",
            contribution_values,
            "RUB; source: brokerage snapshot",
        ),
    )


def write_outputs(root: Path, model: dict) -> None:
    reports = root / "reports"
    package = reports / "chatgpt-export"
    if package.exists():
        shutil.rmtree(package)
    (package / "data").mkdir(parents=True)
    (package / "charts").mkdir()
    (reports / "chatgpt-export.zip").unlink(missing_ok=True)
    for legacy in (reports / "performance.md", reports / "market-summary.json"):
        legacy.unlink(missing_ok=True)
    for legacy in (reports / "data", reports / "charts"):
        if legacy.exists():
            shutil.rmtree(legacy)
    _atomic_write(package / "performance.md", render_performance_report(model))
    _atomic_write(
        package / "market-summary.json",
        json.dumps(build_market_summary(model), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _write_portfolio_csvs(package, model)
    _write_portfolio_charts(package, model)


def export_chatgpt(root: Path) -> None:
    reports = root / "reports"
    destination = reports / "chatgpt-export"
    required = [
        destination / "performance.md",
        destination / "market-summary.json",
        *(destination / "data" / name for name in PORTFOLIO_DATA_FILES),
        *(destination / "charts" / name for name in PORTFOLIO_CHART_FILES),
    ]
    if any(not path.exists() for path in required):
        raise OutputError("Build the report and charts before export-chatgpt")
    archive = reports / "chatgpt-export.zip"
    with tempfile.NamedTemporaryFile(dir=reports, suffix=".zip", delete=False) as handle:
        temporary_archive = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(destination.rglob("*")):
                if path.is_file() and not any(part.startswith(".") for part in path.relative_to(destination).parts):
                    zipped.write(path, Path("chatgpt-export") / path.relative_to(destination))
        temporary_archive.replace(archive)
    finally:
        if temporary_archive.exists():
            temporary_archive.unlink()
