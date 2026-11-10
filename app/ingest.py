"""Import one statement: hash, extract, parse, reconcile, encrypt, store.

Rules:
- A statement is identified by the SHA-256 of its PDF. Re-uploading the same file is refused.
- A statement that reconciles "ok" is stored as accepted and counts in analytics.
- A "warning" or "failed" statement is stored for review and does not count until accepted.
- The PDF is stored encrypted. Plain text is only written to the work directory while
  pdftotext runs, and that file is deleted immediately afterwards.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from . import crypto
from .categories import categorize
from .models import Account, Statement, Transaction
from .parsers import find_parser
from .parsers.base import reconcile
from .parsers.pdftext import extract_layout_text


class DuplicateStatement(Exception):
    def __init__(self, statement_id: int):
        super().__init__(f"already imported as statement {statement_id}")
        self.statement_id = statement_id


class WrongInstitution(Exception):
    pass


@dataclass(frozen=True)
class ImportResult:
    statement_id: int
    status: str
    reconciliation: str
    transaction_count: int
    difference: str
    notes: list[str]


def _pdf_to_text(pdf_bytes: bytes, work_dir: Path) -> str:
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, path = tempfile.mkstemp(dir=work_dir, suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pdf_bytes)
        return extract_layout_text(path)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def import_pdf(
    session: Session,
    *,
    pdf_bytes: bytes,
    original_name: str,
    account: Account,
    uploaded_by_id: int,
    key: bytes,
    pdf_dir: Path,
    work_dir: Path,
) -> ImportResult:
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    existing = session.query(Statement).filter_by(sha256=sha).first()
    if existing is not None:
        raise DuplicateStatement(existing.id)

    text = _pdf_to_text(pdf_bytes, work_dir)
    parser = find_parser(text)  # raises NoParserError if no parser recognises it

    if parser.institution != account.institution:
        raise WrongInstitution(
            f"statement is from {parser.institution}, account is {account.institution}"
        )

    parsed = parser.parse(text)
    result = reconcile(parsed)
    status = "accepted" if result.ok else "review"

    notes = list(parsed.anomalies) + list(result.row_warnings)
    if result.status == "failed":
        notes.insert(0, f"closing balance differs by {result.difference}")

    # Encrypt and store the PDF first, so a database row never points at a missing file.
    pdf_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pdf_file = f"{sha}-{secrets.token_hex(4)}.bin"
    blob = crypto.encrypt(key, pdf_bytes, associated=sha.encode())
    (pdf_dir / pdf_file).write_bytes(blob)

    statement = Statement(
        account_id=account.id,
        uploaded_by_id=uploaded_by_id,
        sha256=sha,
        original_name=original_name[:255],
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        opening_balance=parsed.opening_balance,
        closing_balance=parsed.closing_balance,
        status=status,
        reconciliation=result.status,
        notes="\n".join(notes[:200]),
        pdf_file=pdf_file,
    )
    session.add(statement)
    session.flush()

    for t in parsed.transactions:
        session.add(
            Transaction(
                statement_id=statement.id,
                account_id=account.id,
                booked_on=t.booked_on,
                value_date=t.value_date,
                description=t.description[:255],
                amount=t.amount,
                balance=t.balance,
                category=categorize(t.description, t.amount),
            )
        )
    session.commit()

    return ImportResult(
        statement_id=statement.id,
        status=status,
        reconciliation=result.status,
        transaction_count=len(parsed.transactions),
        difference=str(result.difference),
        notes=notes[:20],
    )


def read_pdf(statement: Statement, key: bytes, pdf_dir: Path) -> bytes:
    blob = (pdf_dir / statement.pdf_file).read_bytes()
    return crypto.decrypt(key, blob, associated=statement.sha256.encode())
