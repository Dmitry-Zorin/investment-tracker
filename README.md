# investment-tracker

Local Python tools for MOEX market data, portfolio performance reports, charts,
and ChatGPT-ready ZIP exports. The code repository contains no real portfolio
data. Runtime data is supplied through an explicit workspace path.

## Requirements

- Python 3.11 or newer
- No third-party Python dependencies

## Commands

`--workspace` is mandatory. The tool does not discover private repositories or
read sibling directories. Run commands from this repository:

```zsh
python3 -m investment_tracker --workspace /path/to/workspace <command>
```

Market data — needs only the manifest and market cache:

- `add SECID --type {fund,bond} --benchmark SECID [--analysis-profile PROFILE]`
  adds a MOEX instrument to the manifest and fetches its history.
- `update` refreshes the cached market CSVs for every enabled instrument.

Portfolio pipeline — also needs the brokerage snapshot and ledger:

- `build` generates the report, summary and charts under `reports/chatgpt-export/`.
- `check` validates the inputs and the generated report package.
- `export-chatgpt` builds `reports/chatgpt-export.zip`.

Market-zone analysis — needs only the manifest and market cache:

- `check-market-analysis` validates the market-analysis inputs.
- `export-market-analysis` builds `reports/market-analysis.zip`.

Supported instrument types are `fund` and `bond`. `--analysis-profile` is one of
`money_market_fund`, `bond_fund`, `floating_rate_bond_fund`, `gold_fund`,
`government_bond`, `generic_fund`, `generic_bond`; it defaults to `generic_fund`
for funds and `generic_bond` for bonds.

## Getting started

`add`/`update` need a manifest to exist first. Create a workspace with an empty
manifest, then populate it:

```zsh
mkdir -p WORKSPACE/data/market
printf '{"schema_version": 1, "instruments": []}\n' > WORKSPACE/data/market/manifest.json
python3 -m investment_tracker --workspace WORKSPACE add SECID --type fund --benchmark BENCH_SECID
python3 -m investment_tracker --workspace WORKSPACE update
```

`build`, `check`, and `export-chatgpt` additionally require
`brokerage-current.json` and `brokerage-ledger.jsonl` in the workspace root.

## Development

Run the offline test suite:

```zsh
python3 -m unittest discover -s tests -v
```

`fixtures/mock-workspace` contains synthetic data for development. Tests copy
it to a temporary directory before generating reports, so tracked fixtures stay
unchanged.

See [workspace-format.md](docs/workspace-format.md),
[privacy.md](docs/privacy.md), and [ai-workflow.md](docs/ai-workflow.md).
