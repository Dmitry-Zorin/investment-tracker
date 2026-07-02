from __future__ import annotations

# Thin re-export facade. The implementation was split into three cohesive
# modules; this module preserves the historical import surface so existing
# callers (market_report, cli, tests) keep working unchanged.

from investment_tracker.performance_model import (
    ASSET_TYPES,
    BENCHMARK_EQUAL_THRESHOLD,
    REQUIRED_POSITION_FIELDS,
    OutputError,
    _first_on_or_after,
    _latest_on_or_before,
    _localize_annualized_reason,
    _safe_float,
    _transactions_for_position,
    _warning,
    aggregate_ticker_views,
    build_market_summary,
    build_report_model,
    interpret_benchmark_difference,
)
from investment_tracker.performance_render import (
    _fmt_money,
    _fmt_percent,
    _md_cell,
    render_bar_chart,
    render_multi_line_chart,
    render_performance_report,
    render_period_returns_chart,
)
from investment_tracker.performance_export import (
    CSV_SCHEMAS,
    PORTFOLIO_CHART_FILES,
    PORTFOLIO_DATA_FILES,
    _write_csv,
    _write_portfolio_charts,
    _write_portfolio_csvs,
    export_chatgpt,
    validate_portfolio_outputs,
    write_outputs,
)

__all__ = [
    "ASSET_TYPES",
    "BENCHMARK_EQUAL_THRESHOLD",
    "CSV_SCHEMAS",
    "OutputError",
    "PORTFOLIO_CHART_FILES",
    "PORTFOLIO_DATA_FILES",
    "REQUIRED_POSITION_FIELDS",
    "_first_on_or_after",
    "_fmt_money",
    "_fmt_percent",
    "_latest_on_or_before",
    "_localize_annualized_reason",
    "_md_cell",
    "_safe_float",
    "_transactions_for_position",
    "_warning",
    "_write_csv",
    "_write_portfolio_charts",
    "_write_portfolio_csvs",
    "aggregate_ticker_views",
    "build_market_summary",
    "build_report_model",
    "export_chatgpt",
    "interpret_benchmark_difference",
    "render_bar_chart",
    "render_multi_line_chart",
    "render_performance_report",
    "render_period_returns_chart",
    "validate_portfolio_outputs",
    "write_outputs",
]
