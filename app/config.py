"""Runtime settings, read from environment variables.

Production (YunoHost) sets these explicitly. The defaults are for local development only.

FINANCE_DATA_DIR      where the database and encrypted PDFs live
FINANCE_DATABASE_URL  SQLAlchemy URL (PostgreSQL on YunoHost)
FINANCE_KEY_FILE      server-managed encryption key; keep it out of the data dir and backups
FINANCE_WORK_DIR      scratch space for PDF text extraction (0700, cleared after each import)
FINANCE_DEV_USER      when set, requests without an SSO header act as this user (development only)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    key_file: Path
    work_dir: Path
    dev_user: str | None

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("FINANCE_DATA_DIR", "./data")).resolve()
        return cls(
            data_dir=data_dir,
            database_url=os.environ.get("FINANCE_DATABASE_URL", f"sqlite:///{data_dir / 'finance.db'}"),
            key_file=Path(os.environ.get("FINANCE_KEY_FILE", str(data_dir / "secret.key"))),
            work_dir=Path(os.environ.get("FINANCE_WORK_DIR", str(data_dir / "work"))),
            dev_user=os.environ.get("FINANCE_DEV_USER") or None,
        )
