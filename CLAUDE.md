# CLAUDE.md

Guidance for Claude Code working in this repository. For the project overview and
full CLI reference see [README.md](README.md); for the non-negotiable working
agreements see [AGENTS.md](AGENTS.md) — follow them.

## What this is

A local, offline-first Python CLI for MOEX market data and portfolio reporting.
Standard library only — `pyproject.toml` keeps `dependencies = []`, do not add any
without explicit approval. Python 3.11+.

The repo holds **no real portfolio data**. All runtime data lives in an external
workspace passed via a mandatory `--workspace`. Develop against
`fixtures/mock-workspace`, never a private workspace — see [docs/privacy.md](docs/privacy.md).

## Commands

```zsh
# run the CLI (mandatory --workspace, then a subcommand)
python3 -m investment_tracker --workspace fixtures/mock-workspace check-market-analysis

# offline test suite — must pass before committing
python3 -m unittest discover -s tests -v

# render charts to PNG for visual review (macOS qlmanage; output in .chart-preview/)
python3 tools/render_charts.py
```

Subcommands: `add`, `update`, `build`, `check`, `export-chatgpt`,
`check-market-analysis`, `export-market-analysis`. Full reference in [README.md](README.md).

## Architecture

Two report pipelines share a MOEX data layer and a workspace boundary.

- `cli.py` / `__main__.py` — argument parsing, dispatch to command handlers, and
  the single place typed errors become `error: <message>` on stderr (exit code 1).
- `workspace.py` — `WorkspacePaths` resolves inputs/outputs and enforces the
  workspace boundary: paths that resolve outside the root, including via symlinks,
  are rejected. All file access goes through it.
- `market_data.py` — MOEX ISS client, manifest + CSV cache, secid validation,
  analysis profiles. Only the configured host is contacted; off-host redirects are
  rejected.
- `io_utils.py` (atomic writes, `sha256_file`, number formatting) and `dates.py`
  (date arithmetic) — shared helpers.

Portfolio pipeline (`add`/`update`/`build`/`check`/`export-chatgpt`):
- `market_report.py` — command handlers and repository validation.
- `performance_calculations.py` → `performance_model.py` → `performance_output.py`
  / `performance_render.py` / `performance_export.py` — compute, model, write
  Markdown/JSON, render charts, package the ZIP.

Market-analysis pipeline (`check-market-analysis`/`export-market-analysis`):
- `market_analysis.py` — command handlers.
- `market_analysis_calculations.py` / `market_analysis_output.py` — compute and render.

Every command writes only beneath the workspace's `reports/`. Layout is documented
in [docs/workspace-format.md](docs/workspace-format.md).

## Conventions

- **Test-first.** Add a failing test for a behavior change, then implement, then
  run the full suite. Tests copy `fixtures/mock-workspace` to a temp dir, so tracked
  fixtures stay unchanged — keep it that way.
- **Stdlib only.** No new dependencies.
- **Scope is v1:** MOEX instruments (`fund`, `bond` — with an `analysis_profile`),
  RUB values, and the current normalized brokerage snapshot/ledger schemas.
- **Charts are SVG; correctness is visual.** After chart changes, render them with
  `tools/render_charts.py` and run `tests/test_chart_geometry.py` before reporting
  them fixed — don't ask the user to run the script to find bugs.
