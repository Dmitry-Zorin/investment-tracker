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
```

The brokerage snapshot and ledger are inputs. `manifest.json` defines enabled
MOEX instruments, their analysis profiles, and each instrument's benchmark.
Every enabled instrument and benchmark must have a matching market CSV.

Commands write only beneath `reports/`, except `add` and `update`, which update
the market manifest and CSV cache. Required inputs and outputs that resolve
outside the workspace through symlinks are rejected.

The current format is intentionally limited to MOEX instruments, RUB values,
and the existing normalized brokerage snapshot and ledger schemas.
