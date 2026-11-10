# yunohost-finance

Self-hosted personal finance app for YunoHost (SSO, multi-user).
Imports bank and card statement PDFs, reconciles every statement to the cent,
and provides categorised analytics and export.

## Layout
- `docs/`        design notes, restore procedure, parser notes
- `app/`         FastAPI application
- `app/parsers/` one module per institution, shared interface
- `app/ingest/`  upload, encryption-at-rest, reconciliation, review queue
- `tests/`       tests on synthetic PDFs only (no real statement data)
- `tests/fixtures_synthetic/` generated sample statements

## Rules
- No real statement data, account numbers or names in git.
- Parsers are validated by balance reconciliation before data is accepted.
