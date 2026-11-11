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
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from .categories import categorize, is_card_payment
from .models import Account, Statement, Transaction
from .parsers import INSTITUTION_NAMES, find_parser
from .parsers.base import reconcile
from .parsers.pdftext import PdfPasswordError, extract_layout_text


class DuplicateStatement(Exception):
    def __init__(self, statement_id: int):
        super().__init__(f"already imported as statement {statement_id}")
        self.statement_id = statement_id


class WrongInstitution(Exception):
    pass


class AccountNeeded(Exception):
    """No account was given, and the owner does not have exactly one account for this bank."""

    def __init__(self, institution: str, candidates: list[int], suggested: dict | None = None):
        super().__init__(f"choose an account for {institution}")
        self.institution = institution
        self.candidates = candidates
        self.suggested = suggested


class WrongAccount(Exception):
    """The statement's account number does not match the account it was uploaded to."""


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


def _match_account(session: Session, account: Account | None, owner_accounts: list[Account],
                   institution: str, ref_last4: str) -> Account:
    """Pick the account a statement belongs to, from its institution and account number."""
    if account is not None:
        if account.institution != institution:
            raise WrongInstitution(f"statement is from {institution}, account is {account.institution}")
        if ref_last4 and account.account_ref_last4 and account.account_ref_last4 != ref_last4:
            raise WrongAccount(f"statement is for the account ending {ref_last4}, "
                               f"this account ends {account.account_ref_last4}")
        if ref_last4 and not account.account_ref_last4:
            account.account_ref_last4 = ref_last4
            session.flush()
        return account

    same_bank = [a for a in owner_accounts if a.institution == institution]
    if ref_last4:
        exact = [a for a in same_bank if a.account_ref_last4 == ref_last4]
        if len(exact) == 1:
            return exact[0]
        # Never assign a statement to an account without a number, even if it is
        # the only one: it may belong to another account of the same bank. The
        # person picks the account, and that choice records its number.
        # Only accounts that could take this statement are offered.
        compatible = [a.id for a in same_bank if not a.account_ref_last4]
        raise AccountNeeded(institution, compatible, _suggest(institution, ref_last4) if not compatible else None)
    if len(same_bank) == 1:
        return same_bank[0]
    raise AccountNeeded(institution, [a.id for a in same_bank], _suggest(institution, "") if not same_bank else None)


def _suggest(institution: str, ref_last4: str) -> dict:
    name = INSTITUTION_NAMES.get(institution, institution)
    label = f"{name} ···{ref_last4}" if ref_last4 else name
    return {"institution": institution, "account_ref_last4": ref_last4, "label": label}


def import_pdf(
    session: Session,
    *,
    pdf_bytes: bytes,
    original_name: str,
    account: Account | None,
    owner_accounts: list[Account],
    uploaded_by_id: int,
    password_candidates: list[str],
    user_rules: list[tuple[str, str]],
    pdf_dir: Path,
    work_dir: Path,
) -> ImportResult:
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    existing = session.query(Statement).filter_by(sha256=sha).first()
    if existing is not None:
        raise DuplicateStatement(existing.id)

    text, password_used = extract_text(pdf_bytes, work_dir, password_candidates)
    parser = find_parser(text)  # raises NoParserError if no parser recognises it


    parsed = parser.parse(text)
    result = reconcile(parsed)

    # Only the last four digits are kept, and only for matching.
    ref_last4 = (parsed.account_ref or "")[-4:]
    account = _match_account(session, account, owner_accounts, parser.institution, ref_last4)
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
        opening_balance=parsed.opening_balance if parsed.opening_balance is not None else Decimal("0.00"),
        closing_balance=parsed.closing_balance if parsed.closing_balance is not None else Decimal("0.00"),
        status=status,
        reconciliation=result.status,
        notes="\n".join(notes[:200]),
        pdf_file=pdf_file,
    )
    session.add(statement)
    session.flush()

    for t in parsed.transactions:
        category, source = categorize(t.description, t.amount, user_rules)
        card_payment = is_card_payment(t.description, t.amount, parser.institution)
        if card_payment:
            category, source = "Card payments", "default"
        session.add(
            Transaction(
                statement_id=statement.id,
                account_id=account.id,
                booked_on=t.booked_on,
                value_date=t.value_date,
                description=t.description[:255],
                amount=t.amount,
                balance=t.balance,
                category=category,
                category_source=source,
                is_transfer=card_payment,
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
