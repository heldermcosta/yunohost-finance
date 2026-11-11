"""Statements stored twice: two accepted statements of one account whose periods overlap and whose
movements repeat.

A movement is identified by its booking date, amount, description (ignoring case and spacing) and the
balance after it. Two statements are flagged when they share at least MIN_SHARE of the movements of the
smaller one. Flagging per movement is not enough: a card repeats the same small charge every month (a
fee, a subscription), and those are real movements, not copies. Comparing whole statements avoids that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

MIN_SHARE = 0.8


@dataclass(frozen=True)
class StatementMovements:
    id: int
    account_id: int
    period_start: date
    period_end: date
    file: str
    signatures: frozenset  # one (booked_on, amount, description, balance) per movement


def movement_signature(booked_on: date, amount, description: str, balance) -> tuple:
    text = " ".join((description or "").upper().split())
    return (booked_on, str(amount), text, None if balance is None else str(balance))


def find_duplicate_statements(statements: list[StatementMovements]) -> list[dict]:
    pairs = []
    for i, a in enumerate(statements):
        for b in statements[i + 1:]:
            if a.account_id != b.account_id or not a.signatures or not b.signatures:
                continue
            if a.period_start > b.period_end or b.period_start > a.period_end:
                continue  # the periods do not overlap
            shared = len(a.signatures & b.signatures)
            smaller = min(len(a.signatures), len(b.signatures))
            if shared and shared / smaller >= MIN_SHARE:
                first, second = sorted((a, b), key=lambda s: (s.period_start, s.id))
                pairs.append({
                    "first": first, "second": second, "shared": shared,
                    "shared_signatures": a.signatures & b.signatures,
                    "identical": a.signatures == b.signatures,
                })
    return pairs


def repeated_amounts(pair: dict) -> tuple[Decimal, Decimal]:
    """(income, spending) counted twice by one pair: the amounts of the movements both copies share."""
    income = spending = Decimal(0)
    for _booked_on, amount, _text, _balance in pair["shared_signatures"]:
        value = Decimal(amount)
        if value > 0:
            income += value
        else:
            spending += -value
    return income, spending
