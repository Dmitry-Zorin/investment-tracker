from __future__ import annotations

import argparse
import sys

from investment_tracker import market_analysis, market_report
from investment_tracker.market_analysis_calculations import MarketAnalysisError
from investment_tracker.market_analysis_output import MarketAnalysisOutputError
from investment_tracker.market_data import ANALYSIS_PROFILES, MarketDataError
from investment_tracker.performance_calculations import CalculationError
from investment_tracker.performance_output import OutputError
from investment_tracker.workspace import WorkspaceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update MOEX data and build portfolio reports for an explicit workspace"
    )
    parser.add_argument("--workspace", required=True, help="workspace containing inputs and reports")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="add a MOEX instrument")
    add.add_argument("secid")
    add.add_argument("--type", required=True, choices=("fund", "bond"))
    add.add_argument("--benchmark", required=True)
    add.add_argument("--analysis-profile", choices=sorted(ANALYSIS_PROFILES))
    add.set_defaults(handler=market_report.command_add)

    commands.add_parser("update", help="update market CSV files").set_defaults(
        handler=market_report.command_update
    )
    commands.add_parser("build", help="build portfolio report, summary and charts").set_defaults(
        handler=market_report.command_build
    )
    commands.add_parser("check", help="validate portfolio inputs and generated files").set_defaults(
        handler=market_report.command_check
    )
    commands.add_parser("export-chatgpt", help="build portfolio export package").set_defaults(
        handler=market_report.command_export_chatgpt
    )
    commands.add_parser("check-market-analysis", help="validate market-analysis inputs").set_defaults(
        handler=market_analysis.command_check
    )
    commands.add_parser("export-market-analysis", help="build market-analysis package").set_defaults(
        handler=market_analysis.command_export_chatgpt
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (
        WorkspaceError,
        MarketDataError,
        CalculationError,
        OutputError,
        MarketAnalysisError,
        MarketAnalysisOutputError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
