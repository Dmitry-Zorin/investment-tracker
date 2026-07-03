from __future__ import annotations

import csv
import json
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, build_opener

from investment_tracker.io_utils import atomic_write, format_number
from investment_tracker.workspace import WorkspacePaths


CSV_FIELDS = (
    "date",
    "board_id",
    "close",
    "price_unit",
    "accrued_interest",
    "unit_value_rub",
    "volume",
    "value_rub",
)
BOND_CSV_FIELDS = CSV_FIELDS + ("yield_close",)

ANALYSIS_PROFILES = {
    "money_market_fund",
    "bond_fund",
    "floating_rate_bond_fund",
    "gold_fund",
    "government_bond",
    "generic_fund",
    "generic_bond",
    "gold_reference",
}

# The (engine, market) pairs each instrument type is allowed to trade on. A
# `reference` is a non-held price series (e.g. the GLDRUB_TOM gold fixing) taken
# from the MOEX currency/selt market rather than a stock board.
SUPPORTED_MARKETS = {
    "fund": {("stock", "shares")},
    "bond": {("stock", "bonds")},
    "reference": {("currency", "selt")},
}


class MarketDataError(RuntimeError):
    pass


# Underscores appear in MOEX currency-market tickers such as GLDRUB_TOM; they
# are safe here because the pattern still excludes path separators and dots, so
# a secid can never traverse out of the workspace.
_SECID_PATTERN = re.compile(r"^[A-Z0-9_]+$")


def _validate_secid(secid: Any) -> str:
    if not isinstance(secid, str) or not _SECID_PATTERN.fullmatch(secid):
        raise MarketDataError(f"Invalid MOEX secid: {secid!r}")
    return secid


def _market_csv_path(root: Path, secid: str) -> Path:
    _validate_secid(secid)
    path = WorkspacePaths(root).market_csv(secid)
    if not path.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
        raise MarketDataError(f"Market CSV path escapes workspace: {secid!r}")
    return path


def default_analysis_profile(instrument_type: str) -> str:
    if instrument_type == "fund":
        return "generic_fund"
    if instrument_type == "bond":
        return "generic_bond"
    if instrument_type == "reference":
        return "gold_reference"
    raise MarketDataError(f"Unsupported instrument type: {instrument_type}")


def history_overlap(existing: list[dict], instrument: dict) -> str | None:
    if not existing:
        return None
    if instrument["type"] == "bond" and "yield_close" not in existing[0]:
        return None
    return (date.fromisoformat(existing[-1]["date"]) - timedelta(days=7)).isoformat()


class _SameHostRedirectHandler(HTTPRedirectHandler):
    """Reject redirects that would leave the configured MOEX host."""

    def __init__(self, host: str):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).hostname != self.host:
            raise MarketDataError(f"MOEX ISS redirect to disallowed host: {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class MoexClient:
    def __init__(self, base_url: str = "https://iss.moex.com/iss", timeout: int = 30):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise MarketDataError(f"Unsupported source base URL scheme: {base_url!r}")
        if not parsed.hostname:
            raise MarketDataError(f"Source base URL has no host: {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Only the configured host is contacted; redirects off it are rejected.
        self._opener = build_opener(_SameHostRedirectHandler(parsed.hostname))

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        query = urlencode(params or {})
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        try:
            with self._opener.open(url, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise MarketDataError(f"MOEX ISS request failed for {url}: {error}") from error


def load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MarketDataError(f"Cannot read manifest {path}: {error}") from error
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("instruments"), list):
        raise MarketDataError("Unsupported or incomplete market manifest")
    for instrument in manifest["instruments"]:
        _validate_secid(instrument.get("secid"))
    return manifest


def save_manifest(path: Path, manifest: dict) -> None:
    atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _table(payload: dict, name: str) -> list[dict]:
    table = payload.get(name)
    if not isinstance(table, dict) or "columns" not in table or "data" not in table:
        raise MarketDataError(f"MOEX response has no {name} table")
    return [dict(zip(table["columns"], row)) for row in table["data"]]


def discover_security(client: MoexClient, secid: str, instrument_type: str) -> dict:
    try:
        supported = SUPPORTED_MARKETS[instrument_type]
    except KeyError as error:
        raise MarketDataError(f"Unsupported instrument type: {instrument_type}") from error
    payload = client.get(
        f"securities/{secid}.json",
        {"iss.meta": "off", "iss.only": "description,boards"},
    )
    descriptions = {row["name"]: row["value"] for row in _table(payload, "description")}
    boards = [
        row
        for row in _table(payload, "boards")
        if (row.get("engine"), row.get("market")) in supported
    ]
    if not boards:
        raise MarketDataError(f"No supported MOEX boards found for {secid}")
    primary = next((row for row in boards if row.get("is_primary") == 1), None)
    if primary is None:
        raise MarketDataError(f"No primary MOEX board found for {secid}")
    if instrument_type == "reference":
        # A reference price has a single authoritative board; there is no board
        # migration to stitch together as there is for a held fund.
        selected_boards = [primary["boardid"]]
    else:
        selected_boards = sorted({row["boardid"] for row in boards if row["market"] == primary["market"]})
    return {
        "secid": secid,
        "instrument_id": descriptions.get("ISIN") or descriptions.get("SECID") or secid,
        "name": descriptions.get("NAME") or descriptions.get("SHORTNAME") or secid,
        "primary_board": primary["boardid"],
        "engine": primary["engine"],
        "market": primary["market"],
        "boards": selected_boards,
    }


def normalize_history_row(raw: dict, board_id: str, instrument_type: str) -> dict | None:
    def field(name: str):
        return raw.get(name, raw.get(name.lower()))

    trade_date = field("TRADEDATE") or field("BEGIN")
    if isinstance(trade_date, str):
        trade_date = trade_date[:10]
    close = field("CLOSE")
    if not trade_date:
        raise MarketDataError(f"History row has no date: {raw}")
    if close is None:
        return None
    close = float(close)
    if instrument_type == "fund":
        accrued_interest = None
        unit_value = close
        price_unit = "rub_per_unit"
    elif instrument_type == "bond":
        accrued_interest = field("ACCINT")
        face_value = field("FACEVALUE")
        if accrued_interest is None or face_value is None:
            raise MarketDataError(f"Bond history row lacks ACCINT or FACEVALUE: {raw}")
        accrued_interest = float(accrued_interest)
        unit_value = float(face_value) * close / 100 + accrued_interest
        price_unit = "percent_of_nominal"
    elif instrument_type == "reference":
        if close <= 0:
            # MOEX reports CLOSE=0 for GLDRUB_TOM on non-trading days. Skip these
            # like the fund no-trade rows (CLOSE=None) rather than persist a zero
            # that later trips the positive-price invariant in market analysis.
            return None
        accrued_interest = None
        unit_value = close
        price_unit = "rub_per_gram"
    else:
        raise MarketDataError(f"Unsupported instrument type: {instrument_type}")
    result = {
        "date": trade_date,
        "board_id": board_id,
        "close": close,
        "price_unit": price_unit,
        "accrued_interest": accrued_interest,
        "unit_value_rub": round(unit_value, 10),
        "volume": field("VOLUME"),
        "value_rub": field("VALUE"),
    }
    if instrument_type == "bond":
        yield_close = field("YIELDCLOSE")
        result["yield_close"] = None if yield_close is None else float(yield_close)
    return result


def merge_board_rows(board_rows: list[list[dict]], primary_board: str) -> tuple[list[dict], list[str]]:
    by_date: dict[str, dict] = {}
    warnings: list[str] = []
    for rows in board_rows:
        for row in rows:
            existing = by_date.get(row["date"])
            if existing is None:
                by_date[row["date"]] = row
                continue
            if float(existing["close"]) != float(row["close"]):
                warnings.append(
                    f"Conflicting close prices on {row['date']}: "
                    f"{existing['board_id']}={existing['close']}, {row['board_id']}={row['close']}"
                )
            if row["board_id"] == primary_board and existing["board_id"] != primary_board:
                by_date[row["date"]] = row
            elif row["board_id"] == existing["board_id"]:
                existing_values = sum(value is not None and value != "" for value in existing.values())
                row_values = sum(value is not None and value != "" for value in row.values())
                if row_values > existing_values:
                    by_date[row["date"]] = row
    return [by_date[key] for key in sorted(by_date)], warnings


def read_market_csv(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = tuple(reader.fieldnames or ())
            if fields not in {CSV_FIELDS, BOND_CSV_FIELDS}:
                raise MarketDataError(f"Unexpected CSV columns in {path}")
            rows = list(reader)
    except OSError as error:
        raise MarketDataError(f"Cannot read {path}: {error}") from error
    numeric = {"close", "accrued_interest", "unit_value_rub", "volume", "value_rub", "yield_close"}
    for row in rows:
        for key in numeric & row.keys():
            if row[key] == "":
                row[key] = None
                continue
            try:
                row[key] = float(row[key])
            except (TypeError, ValueError) as error:
                raise MarketDataError(
                    f"Invalid numeric value {row[key]!r} for {key} in {path.name}"
                ) from error
    # Guarantee chronological order so downstream latest/first-price lookups stay
    # correct even for a hand-edited CSV (ISO date strings sort lexically).
    rows.sort(key=lambda row: row["date"])
    return rows


def adjust_history_for_corporate_actions(rows: list[dict], actions: list[dict] | None) -> list[dict]:
    adjusted = [dict(row) for row in rows]
    for action in sorted(actions or [], key=lambda item: item.get("effective_date", ""), reverse=True):
        if action.get("type") != "split":
            raise MarketDataError(f"Unsupported corporate action: {action.get('type')}")
        if any("yield_close" in row for row in adjusted):
            # A bond's unit_value_rub bundles accrued interest with the clean
            # price; scaling the whole value would corrupt the accrued-interest
            # component, so reject rather than apply a dimensionally wrong split.
            raise MarketDataError("Split adjustment is not supported for bonds")
        effective_date = action.get("effective_date")
        ratio = action.get("ratio")
        if not isinstance(effective_date, str) or not effective_date:
            raise MarketDataError("Split action requires effective_date")
        if not isinstance(ratio, (int, float)) or ratio <= 0:
            raise MarketDataError("Split action requires a positive ratio")
        for row in adjusted:
            if row["date"] < effective_date:
                row["close"] = float(row["close"]) / ratio
                row["unit_value_rub"] = float(row["unit_value_rub"]) / ratio
    return adjusted


def write_market_csv(path: Path, rows: list[dict]) -> None:
    ordered = sorted(rows, key=lambda row: row["date"])
    if len({row["date"] for row in ordered}) != len(ordered):
        raise MarketDataError(f"Duplicate market dates for {path.stem}")
    lines: list[str] = []
    fields = BOND_CSV_FIELDS if any("yield_close" in row for row in ordered) else CSV_FIELDS
    with tempfile.TemporaryFile("w+", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in ordered:
            writer.writerow({key: format_number(row.get(key)) for key in fields})
        handle.seek(0)
        lines.append(handle.read())
    content = "".join(lines)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    atomic_write(path, content)


def _history_rows(client: MoexClient, security: dict, board_id: str, instrument_type: str, start: str | None) -> list[dict]:
    columns = "TRADEDATE,CLOSE,VOLUME,VALUE"
    if instrument_type == "bond":
        columns += ",ACCINT,FACEVALUE,YIELDCLOSE"
    rows: list[dict] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "iss.meta": "off",
            "history.columns": columns,
            "start": offset,
        }
        if start:
            params["from"] = start
        payload = client.get(
            f"history/engines/{security['engine']}/markets/{security['market']}/boards/"
            f"{board_id}/securities/{security['secid']}.json",
            params,
        )
        page = _table(payload, "history")
        if not page:
            break
        for row in page:
            normalized = normalize_history_row(row, board_id, instrument_type)
            if normalized is not None:
                rows.append(normalized)
        offset += len(page)
        cursor = _table(payload, "history.cursor")
        if not cursor or offset >= int(cursor[0]["TOTAL"]):
            break
    return rows


def select_history_boards(security: dict, instrument_type: str) -> list[str]:
    if instrument_type in {"bond", "reference"}:
        return [security["primary_board"]]
    if instrument_type == "fund":
        selected = {security["primary_board"]}
        selected.update(board for board in security["boards"] if board in {"TQTF", "TQBR"})
        return sorted(selected)
    raise MarketDataError(f"Unsupported instrument type: {instrument_type}")


def update_instrument(client: MoexClient, root: Path, instrument: dict) -> tuple[dict, list[str]]:
    _validate_secid(instrument.get("secid"))
    security = discover_security(client, instrument["secid"], instrument["type"])
    if instrument.get("instrument_id") and instrument["instrument_id"] != security["instrument_id"]:
        raise MarketDataError(f"Instrument id mismatch for {instrument['secid']}")
    path = _market_csv_path(root, instrument["secid"])
    existing = read_market_csv(path) if path.exists() else []
    overlap = history_overlap(existing, instrument)
    fetched = [
        _history_rows(client, security, board, instrument["type"], overlap)
        for board in select_history_boards(security, instrument["type"])
    ]
    recent, warnings = merge_board_rows(fetched, security["primary_board"])
    merged, merge_warnings = merge_board_rows([existing, recent], security["primary_board"])
    warnings.extend(merge_warnings)
    changed = merged != existing
    write_market_csv(path, merged)
    metadata = dict(instrument)
    metadata.update(security)
    metadata["first_market_date"] = merged[0]["date"] if merged else None
    metadata["latest_market_date"] = merged[-1]["date"] if merged else None
    metadata["source"] = "MOEX ISS"
    metadata["warnings"] = warnings
    if changed or not metadata.get("loaded_at"):
        metadata["loaded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return metadata, warnings


def add_instrument(
    client: MoexClient,
    root: Path,
    secid: str,
    instrument_type: str,
    benchmark: str | None,
    analysis_profile: str | None = None,
) -> None:
    _validate_secid(secid)
    manifest_path = WorkspacePaths(root).market_manifest
    manifest = load_manifest(manifest_path)
    if any(item["secid"] == secid for item in manifest["instruments"]):
        raise MarketDataError(f"Instrument {secid} already exists")
    profile = analysis_profile or default_analysis_profile(instrument_type)
    if profile not in ANALYSIS_PROFILES:
        raise MarketDataError(f"Unsupported analysis profile: {profile}")
    candidate = {
        "secid": secid,
        "type": instrument_type,
        "benchmark": benchmark,
        "analysis_profile": profile,
        "enabled": True,
    }
    metadata, _ = update_instrument(client, root, candidate)
    manifest["instruments"].append(metadata)
    manifest["instruments"].sort(key=lambda item: item["secid"])
    save_manifest(manifest_path, manifest)
