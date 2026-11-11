"""Find transfers between the owner's accounts, so they are not counted as spending twice.

A transfer is a debit in one account and a credit of the same amount in another account,
booked within a few days, where at least one description looks like a transfer or a card
payment. Matches are only suggestions in the sense that a person can unmark them.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy.orm import Session

from .models import Statement, Transaction

WINDOW_DAYS = 5
HINTS = ("TRF", "TRANSF", "PAGAMENTO", "PAG CART", "PAG.CART", "CARTAO", "CARTÃO", "MB WAY", "MBWAY")


def _looks_like_transfer(description: str) -> bool:
    text = (description or "").upper()
    return any(h in text for h in HINTS)


def link(a: Transaction, b: Transaction) -> None:
    a.is_transfer = True
    b.is_transfer = True
    a.transfer_id = b.id
    b.transfer_id = a.id


def unlink(t: Transaction, session: Session) -> None:
    partner_id = t.transfer_id
    t.is_transfer = False
    t.transfer_id = None
    if partner_id is not None:
        partner = session.get(Transaction, partner_id)
        if partner is not None and partner.transfer_id == t.id:
            partner.is_transfer = False
            partner.transfer_id = None


def detect_transfers(session: Session, account_ids: list[int]) -> int:
    """Links unmatched transfer pairs among these accounts. Returns the number of pairs made."""
    if not account_ids:
        return 0
    rows = (
        session.query(Transaction)
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(
            Transaction.account_id.in_(account_ids),
            Statement.status == "accepted",
            Transaction.is_transfer.is_(False),
        )
        .order_by(Transaction.booked_on, Transaction.id)
        .all()
    )
    credits_by_amount: dict[Decimal, list[Transaction]] = defaultdict(list)
    for t in rows:
        if t.amount > 0:
            credits_by_amount[t.amount].append(t)

    pairs = 0
    for debit in rows:
        if debit.amount >= 0 or debit.is_transfer:
            continue
        for credit in credits_by_amount.get(-debit.amount, []):
            if credit.is_transfer or credit.account_id == debit.account_id:
                continue
            if abs((credit.booked_on - debit.booked_on).days) > WINDOW_DAYS:
                continue
            if not (_looks_like_transfer(debit.description) or _looks_like_transfer(credit.description)):
                continue
            link(debit, credit)
            pairs += 1
            break
    session.commit()
    return pairs
