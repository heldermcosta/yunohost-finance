"""Import one statement: hash, extract, parse, reconcile, encrypt, store.

Rules:
- A statement is identified by the SHA-256 of its PDF. Re-uploading the same file is refused.
- A statement that reconciles "ok" is stored as accepted and counts in analytics.
- A "warning" or "failed" statement is stored for review and does not count until accepted.
- The PDF is stored as uploaded (not encrypted). Extracted text is only written to the work
  directory while pdftotext runs, and that file is deleted immediately afterwards.
- A password-protected PDF is opened with the uploader's saved passwords, then with the
  password sent with the upload. If none works, PasswordRequired is raised.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from .categories import categorize
from .models import Account, Statement, Transaction
from .parsers import find_parser
from .parsers.base import reconcile
from .parsers.pdftext import PdfPasswordError, extract_layout_text


class DuplicateStatement(Exception):
    def __init__(self, statement_id: int):
        super().__init__(f"already imported as statement {statement_id}")
        self.statement_id = statement_id


class WrongInstitution(Exception):
    pass


class PasswordRequired(Exception):
    """The PDF is protected and none of the known passwords opened it."""


@dataclass(frozen=True)
class ImportResult:
    statement_id: int
    status: str
    reconciliation: str
    transaction_count: int
    difference: str
    notes: list[str]
    # The password that opened the PDF, if one was needed. The API removes it before replying.
    pdf_password: str | None = None


def _pdf_to_text(pdf_bytes: bytes, work_dir: Path, password: str | None = None) -> str:
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, path = tempfile.mkstemp(dir=work_dir, suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pdf_bytes)
        return extract_layout_text(path, password)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def extract_text(pdf_bytes: bytes, work_dir: Path, candidates: list[str]) -> tuple[str, str | None]:
    """Returns (text, password_that_worked). Tries no password first, then each candidate."""
    try:
        return _pdf_to_text(pdf_bytes, work_dir), None
    except PdfPasswordError:
        pass
    for password in candidates:
        try:
            return _pdf_to_text(pdf_bytes, work_dir, password), password
        except PdfPasswordError:
            continue
    raise PasswordRequired()


def import_pdf(
    session: Session,
    *,
    pdf_bytes: bytes,
    original_name: str,
    account: Account,
    uploaded_by_id: int,
    password_candidates: list[str],
    pdf_dir: Path,
    work_dir: Path,
) -> ImportResult:
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    existing = session.query(Statement).filter_by(sha256=sha).first()
    if existing is not None:
        raise DuplicateStatement(existing.id)

    text, password_used = extract_text(pdf_bytes, work_dir, password_candidates)
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

    # Store the PDF first, so a database row never points at a missing file.
    pdf_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pdf_file = f"{sha[:16]}-{secrets.token_hex(4)}.pdf"
    (pdf_dir / pdf_file).write_bytes(pdf_bytes)

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
        pdf_password=password_used,
    )


def read_pdf(statement: Statement, key: bytes, pdf_dir: Path) -> bytes:
    """Files imported before plain storage were encrypted with a .bin suffix; those still open."""
    data = (pdf_dir / statement.pdf_file).read_bytes()
    if statement.pdf_file.endswith(".bin"):
        from . import crypto

        return crypto.decrypt(key, data, associated=statement.sha256.encode())
    return data
