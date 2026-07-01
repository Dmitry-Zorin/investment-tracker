# Work-AI workflow

Open only this repository in the coding assistant. Use files under
`fixtures/mock-workspace` and `tests` for development.

The assistant must not search parent or sibling directories, infer a private
workspace location, or request real financial data to implement code changes.
Run live portfolio generation separately by passing the private workspace path
to the reviewed local CLI.

Before sharing or publishing changes, inspect every tracked file and Git object
for private paths, identifiers, balances, transaction dates, and generated
reports.
