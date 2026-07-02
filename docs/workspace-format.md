# Workspace format

The CLI accepts one explicit workspace root with this layout:

```text
workspace/
  brokerage-current.json
  brokerage-ledger.jsonl
  data/
    market/
      manifest.json
      <SECID>.csv
  reports/
    chatgpt-export/       # build output: performance.md, market-summary.json, data/, charts/
    chatgpt-export.zip    # export-chatgpt package
    market-analysis/      # export-market-analysis output
    market-analysis.zip
```

The brokerage snapshot and ledger are inputs. `manifest.json` defines enabled
MOEX instruments, their analysis profiles, and each instrument's benchmark.
Every enabled instrument and benchmark must have a matching market CSV.

Instruments are limited to two types, `fund` and `bond`. Each instrument carries
an `analysis_profile` — one of `money_market_fund`, `bond_fund`,
`floating_rate_bond_fund`, `gold_fund`, `government_bond`, `generic_fund`,
`generic_bond` — which `add` defaults to `generic_fund` or `generic_bond` by type.

`add` and `update` populate the manifest and CSV cache and require only the
`data/market` tree, not the brokerage files. Every other command writes only
beneath `reports/`. Required inputs and outputs that resolve outside the
workspace through symlinks are rejected.

The current format is intentionally limited to MOEX instruments, RUB values,
and the existing normalized brokerage snapshot and ledger schemas.
