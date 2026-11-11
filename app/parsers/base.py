"""Shared types and the reconciliation check used by every institution parser.

Money is always `Decimal`. Transaction amounts are signed: credits are positive,
debits are negative. A statement is only accepted when its transactions
reconcile to the printed balances (see `reconcile`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class Transaction:
    booked_on: date      # data de lançamento
    value_date: date     # data-valor
    description: str
    amount: Decimal      # signed: credit > 0, debit < 0
    balance: Decimal     # running balance printed on this line


@dataclass
class ParsedStatement:
    institution: str
    account_ref: str     # account number as printed; masked before storage
    holder_label: str    # account name as printed
    currency: str
    period_start: date
    period_end: date
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    transactions: list[Transaction]
    anomalies: list[str] = field(default_factory=list)
    # "bank": a credit raises the balance. "card": a purchase raises what is owed.
    balance_direction: str = "bank"
    # Portfolio snapshot for investment reports: total assets, cash, term deposits.
    snapshot: dict | None = None


@dataclass
class Reconciliation:
    status: str                  # "ok" | "warning" | "failed"
    expected_closing: Decimal
    difference: Decimal          # printed closing minus computed closing
    row_warnings: list[str]      # rows whose printed running balance does not follow

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class StatementParser(Protocol):
    """One implementation per institution and statement layout."""

    institution: str

    def can_parse(self, text: str) -> bool: ...

    def parse(self, text: str) -> ParsedStatement: ...


def reconcile(st: ParsedStatement) -> Reconciliation:
    """Check that the statement adds up.

    1. opening + sum(all amounts) == closing. This decides "failed".
    2. For every row, previous running balance + amount == printed balance.
       A break here makes the statement "warning": the totals agree, but a
       printed balance does not follow, so a person should look at it.

    status "ok" means both checks pass.
    """
    if st.opening_balance is None or st.closing_balance is None:
        return Reconciliation(
            status="failed",
            expected_closing=Decimal("0.00"),
            difference=Decimal("0.00"),
            row_warnings=["balances are not printed on this statement"],
        )

    card = st.balance_direction == "card"
    total = sum((t.amount for t in st.transactions), Decimal("0.00"))
    # Card: the amount owed goes up with a purchase (a negative amount here).
    expected = st.opening_balance - total if card else st.opening_balance + total
    diff = st.closing_balance - expected

    row_warnings: list[str] = []
    prev = st.opening_balance
    for i, t in enumerate(st.transactions, start=1):
        step = prev - t.amount if card else prev + t.amount
        if step != t.balance:
            row_warnings.append(
                f"row {i} ({t.booked_on}): running balance does not follow"
            )
        prev = t.balance

    if diff != 0:
        status = "failed"
    elif row_warnings:
        status = "warning"
    else:
        status = "ok"

    return Reconciliation(
        status=status,
        expected_closing=expected,
        difference=diff,
        row_warnings=row_warnings,
    )
