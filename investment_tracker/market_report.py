from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from investment_tracker.market_data import (
    MoexClient,
    add_instrument,
    load_manifest,
    read_market_csv,
    save_manifest,
    update_instrument,
)
from investment_tracker.performance_calculations import load_ledger
from investment_tracker.performance_output import (
    OutputError,
    build_report_model,
    export_chatgpt,
    validate_portfolio_outputs,
    write_outputs,
)
from investment_tracker.workspace import WorkspacePaths


def _root(args: argparse.Namespace) -> Path:
    workspace = WorkspacePaths.from_path(args.workspace)
    workspace.validate_portfolio_inputs()
    return workspace.root


def command_add(args: argparse.Namespace) -> int:
    root = _root(args)
    manifest = load_manifest(root / "data/market/manifest.json")
    client = MoexClient(manifest.get("source_base_url", "https://iss.moex.com/iss"))
    add_instrument(
        client,
        root,
        args.secid.upper(),
        args.type,
        args.benchmark,
        args.analysis_profile,
    )
    print(f"Added {args.secid.upper()}")
    return 0


def command_update(args: argparse.Namespace) -> int:
    root = _root(args)
    path = root / "data/market/manifest.json"
    manifest = load_manifest(path)
    client = MoexClient(manifest.get("source_base_url", "https://iss.moex.com/iss"))
    updated = []
    warnings = []
    for instrument in manifest["instruments"]:
        if not instrument.get("enabled", True):
            updated.append(instrument)
            continue
        metadata, instrument_warnings = update_instrument(client, root, instrument)
        updated.append(metadata)
        warnings.extend(instrument_warnings)
        print(f"Updated {instrument['secid']}: {metadata.get('first_market_date')}..{metadata.get('latest_market_date')}")
    manifest["instruments"] = sorted(updated, key=lambda item: item["secid"])
    save_manifest(path, manifest)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def command_build(args: argparse.Namespace) -> int:
    root = _root(args)
    model = build_report_model(root)
    write_outputs(root, model)
    print(f"Built reports/performance.md for {model['latest_market_date']}")
    return 0


def _check_repository(root: Path) -> list[str]:
    errors = []
    manifest = load_manifest(root / "data/market/manifest.json")
    secids = set()
    latest_dates = []
    for instrument in manifest["instruments"]:
        secid = instrument.get("secid")
        if not secid or secid in secids:
            errors.append(f"duplicate or missing secid: {secid}")
            continue
        secids.add(secid)
        if not instrument.get("enabled", True):
            continue
        path = root / "data/market" / f"{secid}.csv"
        if not path.exists():
            errors.append(f"missing market CSV: {path.name}")
            continue
        rows = read_market_csv(path)
        dates = [row["date"] for row in rows]
        if not dates:
            errors.append(f"empty market CSV: {path.name}")
            continue
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            errors.append(f"unsorted or duplicate dates: {path.name}")
        latest_dates.append(dates[-1])
    load_ledger(root / "brokerage-ledger.jsonl")
    try:
        brokerage = json.loads((root / "brokerage-current.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"invalid brokerage-current.json: {error}")
    else:
        for check in brokerage.get("checks", []):
            if check.get("status") != "pass":
                errors.append(f"brokerage check failed: {check.get('check_id', 'unknown')}")
    if latest_dates:
        oldest_latest = min(date.fromisoformat(value) for value in latest_dates)
        age = (date.today() - oldest_latest).days
        if age > 30:
            errors.append(f"market data are stale by {age} days")
    summary_path = root / "reports/market-summary.json"
    if summary_path.exists() and latest_dates:
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary_latest = summary.get("metadata", {}).get(
                "last_market_quote_date", summary.get("latest_market_date")
            )
            if summary_latest != max(latest_dates):
                errors.append("generated report is not aligned with current market CSV files")
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid market-summary.json: {error}")
    if summary_path.exists():
        errors.extend(validate_portfolio_outputs(root))
    return errors


def command_check(args: argparse.Namespace) -> int:
    errors = _check_repository(_root(args))
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("Investment workspace checks passed")
    return 0


def command_export_chatgpt(args: argparse.Namespace) -> int:
    root = _root(args)
    errors = _check_repository(root)
    non_stale_errors = [error for error in errors if "stale" not in error]
    if non_stale_errors:
        raise OutputError("Cannot export: " + "; ".join(non_stale_errors))
    export_chatgpt(root)
    print("Built reports/chatgpt-export.zip")
    return 0
