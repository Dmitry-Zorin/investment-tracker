from __future__ import annotations

import csv
import io
import json
import shutil
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from investment_tracker.io_utils import atomic_write, sha256_file
from investment_tracker.performance_model import OutputError, build_market_summary
from investment_tracker.performance_render import (
    render_bar_chart,
    render_multi_line_chart,
    render_performance_report,
    render_period_returns_chart,
)
from investment_tracker.performance_render import _fmt_money, _fmt_percent
from investment_tracker.workspace import WorkspacePaths


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
    "instruments-vs-benchmark.svg",
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
    reports = WorkspacePaths(root).reports
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
        elif not isinstance(digest, str) or digest != sha256_file(path):
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
    atomic_write(path, buffer.getvalue())


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
    atomic_write(
        charts / "portfolio-composition.svg",
        render_bar_chart(
            f"Portfolio composition at {model['brokerage_snapshot_date']}",
            composition,
            "RUB; source: brokerage snapshot",
        ),
    )
    atomic_write(
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
    atomic_write(
        charts / "positions-vs-benchmark.svg",
        render_bar_chart(
            "Positions vs configured benchmark (lot-based; not a trading signal)",
            benchmark_values or [("not available", 0)],
            "percentage points; sources: brokerage entries + MOEX",
        ),
    )
    atomic_write(
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
    atomic_write(
        charts / "pnl-contribution.svg",
        render_bar_chart(
            "Confirmed PnL contribution",
            contribution_values,
            "RUB; source: brokerage snapshot",
        ),
    )
    atomic_write(
        charts / "instruments-vs-benchmark.svg",
        render_multi_line_chart(
            "Instrument performance vs benchmark (indexed to 100)",
            model.get("normalized_series", {}),
            "normalized",
        ),
    )


def write_outputs(root: Path, model: dict) -> None:
    reports = WorkspacePaths(root).reports
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
    atomic_write(package / "performance.md", render_performance_report(model))
    atomic_write(
        package / "market-summary.json",
        json.dumps(build_market_summary(model), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _write_portfolio_csvs(package, model)
    _write_portfolio_charts(package, model)


def export_chatgpt(root: Path) -> None:
    reports = WorkspacePaths(root).reports
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
