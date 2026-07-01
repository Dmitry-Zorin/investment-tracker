# investment-tracker

Local Python tools for MOEX market data, portfolio performance reports, charts,
and ChatGPT-ready ZIP exports. The code repository contains no real portfolio
data. Runtime data is supplied through an explicit workspace path.

## Requirements

- Python 3.11 or newer
- No third-party Python dependencies

## Commands

Run commands from this repository:

```zsh
python3 -m investment_tracker --workspace /path/to/workspace build
python3 -m investment_tracker --workspace /path/to/workspace check
python3 -m investment_tracker --workspace /path/to/workspace export-chatgpt
python3 -m investment_tracker --workspace /path/to/workspace check-market-analysis
python3 -m investment_tracker --workspace /path/to/workspace export-market-analysis
```

`--workspace` is mandatory. The tool does not discover private repositories or
read sibling directories.

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
