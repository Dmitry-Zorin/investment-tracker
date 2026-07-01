# Privacy boundary

This repository is safe for mock-driven development only when it contains no
real portfolio inputs or derived outputs.

Keep these outside the repository:

- brokerage accounts, transactions, quantities, costs, and trade dates;
- personal portfolio state, plans, strategy, and instrument notes;
- the active portfolio's market manifest and cache;
- generated reports, charts, and export archives.

Public market facts may be used, but examples must not reproduce a real
portfolio association. The bundled fixture is synthetic.

Directory separation reduces accidental disclosure; it is not an operating
system security boundary. Do not open this repository together with a private
workspace or a common parent directory in an AI coding workspace.
