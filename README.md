# yunohost-finance

Self-hosted personal finance app for YunoHost, with SSO and sharing between household members.
It imports bank and card statement PDFs, reconciles each one to the cent, and provides
categorised monthly analytics and exports. The app is the source of truth for the statements
once they are imported.

## Status

- Parser for ActivoBank statements (tested on synthetic statements; validated on the real
  statement folder, see docs/parser-validation.md). Other banks and cards are not built yet.
- Import: several PDFs at once, each reconciled. The account is chosen from the bank and the
  last four digits of the account number printed on the statement. Protected PDFs use saved
  passwords, or ask for one. Duplicates are refused.
- Categories: built-in keywords, then your own rules (text contains → category), then manual
  choices. Changing one transaction can apply to similar ones and save a rule.
- Transfers between your accounts (and card payments) are found and kept out of spending.
- Budgets per category per month, with alerts at 80% and 100%.
- Views: overview by month and category, transactions with filters, review queue, accounts
  and sharing, household accounts.
- Exports: CSV, Excel, PDF report by year, full JSON backup.
- Interface in English and Portuguese.
- Not yet done: parsers for the other institutions, the full category rule set from the
  previous importer, and restore verified on a server.

## YunoHost package

Packaging v2, helpers v2.1, targeting YunoHost 12.x with PostgreSQL. The package fetches
the source from a tagged GitHub archive, runs the app from a virtualenv under the install
directory, and serves it through nginx behind SSOwat.

Before it can be installed:
1. Publish tag `v0.1.0` and fill in the archive's sha256 in `package/manifest.toml`.
2. Choose the licence (`[upstream] license`).
3. Test on a YunoHost test server (install, upgrade, backup, restore, remove).

Items marked `VERIFY` in `package/scripts/backup` and `package/scripts/restore` depend on
YunoHost behaviour that was not confirmed in the documentation:
- that the postgresql resource backs up and restores the database automatically;
- the exact usage of `ynh_backup` and `ynh_restore` with a directory argument;
- `ynh_die` and `ynh_script_progression --last`.

Encryption key: created at `/etc/<app>/secret.key` on install, excluded from backups. On a
restore to a new server, the key from the original server must be copied there first; the
restore script stops with a message if it is missing.

SSO: SSOwat sends the username in `Authorization: Basic …` when the permission has
`auth_header = true`. The app reads only the username. Never expose its port directly.

## Run locally (development)

    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements-dev.txt
    FINANCE_DEV_USER=helder uvicorn app.main:create_app --factory --reload
    pytest

Requires `pdftotext` (poppler-utils) on the PATH.

## Configuration (environment)

| Variable | Purpose |
|---|---|
| `FINANCE_DATA_DIR` | database and encrypted PDFs |
| `FINANCE_DATABASE_URL` | SQLAlchemy URL (PostgreSQL on YunoHost) |
| `FINANCE_KEY_FILE` | server-managed encryption key, outside the data dir and backups |
| `FINANCE_WORK_DIR` | scratch space for PDF text extraction, cleared after each import |
| `FINANCE_DEV_USER` | development only: acts as this user when no SSO header is sent |

## Security model

- Statement PDFs are stored as uploaded, without encryption, in the data directory. Back up
  the data directory with the database.
- PDF passwords are stored encrypted (AES-256-GCM) with the server key, and are never returned
  by the API. Each user has their own saved passwords.
- The key is created once with `O_EXCL` and mode 0600, and is never written to the database.
  It is needed to open saved passwords and statements imported before plain storage (`.bin`).
  Keep a copy off the server: without it, saved passwords cannot be decrypted.
- Plain text from a PDF exists only in the work directory during extraction, then it is deleted.
- Access: an account is visible to its owner and to users it was shared with. Only the owner
  can share, accept or delete a statement.
- Authentication comes from YunoHost SSO: SSOwat sends `Authorization: Basic` with the username,
  and the app reads only the username. Never expose the app's port directly.

## Open decisions

- **View scope.** The two options "own and shared" and "all" currently select the same
  accounts, because a user can only access their own and shared accounts. If "all" should mean
  something wider (for example every household account), the access rule needs to change.
- **Review rules.** A statement with a failed total is never accepted automatically. A
  statement whose totals agree but whose running balances do not chain goes to review as a
  warning.
