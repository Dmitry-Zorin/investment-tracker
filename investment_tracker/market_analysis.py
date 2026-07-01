#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from investment_tracker.market_analysis_calculations import MarketAnalysisError  # noqa: E402
from investment_tracker.market_analysis_output import (  # noqa: E402
    MarketAnalysisOutputError,
    build_market_analysis_model,
    export_market_analysis,
    validate_market_analysis_model,
)
from investment_tracker.market_data import MarketDataError  # noqa: E402
from investment_tracker.workspace import WorkspacePaths


def _root(args: argparse.Namespace) -> Path:
    workspace = WorkspacePaths.from_path(args.workspace)
    workspace.validate_market_inputs()
    return workspace.root


def command_check(args: argparse.Namespace) -> int:
    errors = validate_market_analysis_model(build_market_analysis_model(_root(args)))
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("Market zone analysis checks passed")
    return 0


def command_export_chatgpt(args: argparse.Namespace) -> int:
    root = _root(args)
    model = build_market_analysis_model(root)
    errors = validate_market_analysis_model(model)
    if errors:
        raise MarketAnalysisOutputError("Cannot export: " + "; ".join(errors))
    path = export_market_analysis(root, model)
    print(f"Built {path.relative_to(root)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build ChatGPT-ready market zone analysis")
    parser.add_argument("--workspace", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").set_defaults(handler=command_check)
    commands.add_parser("export-chatgpt").set_defaults(handler=command_export_chatgpt)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (MarketDataError, MarketAnalysisError, MarketAnalysisOutputError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
