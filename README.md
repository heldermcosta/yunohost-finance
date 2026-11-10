# yunohost-finance

Self-hosted personal finance app for YunoHost, with SSO and sharing between household members.
It imports bank and card statement PDFs, reconciles each one to the cent, and provides
categorised monthly analytics and exports. The app is the source of truth for the statements
once they are imported.

## Status

- Parser framework and ActivoBank parser (tested on synthetic statements; validated on the
  real statement folder, see docs/parser-validation.md).
- App skeleton: database models, encrypted PDF storage, import pipeline with a review queue,
  access rules, monthly summary, CSV export.
- YunoHost package drafted in `package/` (manifest, install, upgrade, remove, backup,
  restore, systemd and nginx templates). Not yet tested on a YunoHost server.
- Not yet done: web front end, the other institutions' parsers, the full category rule set.

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

- PDFs are encrypted at rest with AES-256-GCM. Each file is bound to its SHA-256.
- The key is created once with `O_EXCL` and mode 0600, and is never written to the database.
- Plain text from a PDF exists only in the work directory during extraction, then it is deleted.
- Access: an account is visible to its owner and to users it was shared with. Only the owner
  can share, accept or delete a statement.
- Authentication comes from the SSO header `Auth-User`. This must be verified against the
  YunoHost SSOwat configuration before deployment.

## Open decisions

- **View scope.** The two options "own and shared" and "all" currently select the same
  accounts, because a user can only access their own and shared accounts. If "all" should mean
  something wider (for example every household account), the access rule needs to change.
- **Review rules.** A statement with a failed total is never accepted automatically. A
  statement whose totals agree but whose running balances do not chain goes to review as a
  warning.
