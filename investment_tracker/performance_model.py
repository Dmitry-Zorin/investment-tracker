from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from investment_tracker.io_utils import sha256_file
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
from investment_tracker.workspace import WorkspacePaths


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


def _warning(severity: str, code: str, message: str, affected_items: list[str] | None = None) -> dict:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "affected_items": affected_items or [],
    }


BENCHMARK_EQUAL_THRESHOLD = 0.0005


def interpret_benchmark_difference(difference: float | None) -> str:
    if difference is None:
        return "not_available"
    if abs(difference) <= BENCHMARK_EQUAL_THRESHOLD:
        return "approximately_equal"
    if difference > 0:
        return "outperformed_benchmark"
    return "underperformed_benchmark"


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
        interpretation = interpret_benchmark_difference(difference)
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
    paths = WorkspacePaths(root)
    manifest = load_manifest(paths.market_manifest)
    try:
        brokerage = json.loads((root / "brokerage-current.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OutputError(f"Cannot read brokerage-current.json: {error}") from error
    ledger = load_ledger(root / "brokerage-ledger.jsonl")
    by_id = {item["instrument_id"]: item for item in manifest["instruments"] if item.get("enabled")}
    histories = {}
    for item in by_id.values():
        raw_rows = read_market_csv(paths.market_csv(item["secid"]))
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
        interpretation = interpret_benchmark_difference(difference)
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
        paths.market_manifest,
    ]
    source_paths.extend(paths.market_csv(item["secid"]) for item in by_id.values())
    roles = {
        "brokerage-current.json": "brokerage_snapshot",
        "brokerage-ledger.jsonl": "brokerage_ledger",
        "data/market/manifest.json": "market_manifest",
    }
    generated_from = [
        {
            "path": str(path.relative_to(root)),
            "sha256": sha256_file(path),
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
        "corporate_actions": [
            {"secid": item["secid"], **action}
            for item in by_id.values()
            for action in item.get("corporate_actions", [])
        ],
        "generated_from": generated_from,
    }


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
