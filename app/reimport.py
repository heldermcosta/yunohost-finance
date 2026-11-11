"""Read a stored statement again, and bring in only what is new or missing.

Used when a parser has been improved, so statements already imported can pick up
what the new reading finds. The data a person has added or changed is kept.

What is changed, and only when the new reading reconciles:

- A stored transaction with a blank description takes the description read now.
  Its date is also taken from the new reading, because a blank-description row
  was dated by a guess when it was first imported.
- A transaction read now but not stored yet is added, categorised like a new import.

What is never changed: categories and how they were set, object names, transfer
links, balances, value dates, and the statement's own totals and status. A stored
transaction with a different, non-blank description, or a different date, is listed
so it can be checked by hand, not overwritten. A stored transaction the new reading
no longer finds is listed for review, never deleted.

Matching: a stored and a read transaction are the same row when the amount is equal
and the dates are at most MAX_DATE_GAP_DAYS apart. Among several, the nearest date
wins, then the same description.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from .ingest import extract_text, new_transaction, repair_text
from .models import Account, Statement, Transaction
from .parsers import find_parser
from .parsers.base import ParsedStatement, Transaction as ReadTransaction, reconcile

MAX_DATE_GAP_DAYS = 10


class ReimportRefused(Exception):
    """The statement cannot be reread safely (for example, the new reading does not reconcile)."""


@dataclass
class ReimportPlan:
    reconciles: bool
    matched: int = 0
    fill_descriptions: list[tuple[Transaction, str]] = field(default_factory=list)
    move_dates: list[tuple[Transaction, date, date]] = field(default_factory=list)
    differs: list[tuple[Transaction, ReadTransaction]] = field(default_factory=list)
    to_add: list[ReadTransaction] = field(default_factory=list)
    to_review: list[Transaction] = field(default_factory=list)
    totals_differ: list[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.fill_descriptions) + len(self.move_dates) + len(self.to_add)


def _same_row(stored: Transaction, read: ReadTransaction) -> int | None:
    """Distance in days for a possible match, or None when the two cannot be the same row."""
    if stored.amount != read.amount:
        return None
    gap = abs((stored.booked_on - read.booked_on).days)
    return gap if gap <= MAX_DATE_GAP_DAYS else None


def plan_reimport(session: Session, statement: Statement, parsed: ParsedStatement,
                  reconciles: bool) -> ReimportPlan:
    plan = ReimportPlan(reconciles=reconciles)
    stored = session.query(Transaction).filter_by(statement_id=statement.id).order_by(Transaction.id).all()

    used: set[int] = set()
    pairs: list[tuple[Transaction, ReadTransaction]] = []
    for read in parsed.transactions:
        best = None
        for s in stored:
            if s.id in used:
                continue
            gap = _same_row(s, read)
            if gap is None:
                continue
            key = (gap, (s.description or "").strip() != read.description.strip())
            if best is None or key < best[0]:
                best = (key, s)
        if best is None:
            plan.to_add.append(read)
        else:
            used.add(best[1].id)
            pairs.append((best[1], read))
    plan.to_review = [s for s in stored if s.id not in used]
    plan.matched = len(pairs)

    for s, read in pairs:
        stored_text = (s.description or "").strip()
        read_text = read.description.strip()
        if not stored_text and read_text:
            plan.fill_descriptions.append((s, read.description[:255]))
            if read.booked_on != s.booked_on:
                plan.move_dates.append((s, s.booked_on, read.booked_on))
        elif stored_text != read_text or read.booked_on != s.booked_on:
            plan.differs.append((s, read))

    if statement.opening_balance is not None and parsed.opening_balance is not None \
            and Decimal(statement.opening_balance) != parsed.opening_balance:
        plan.totals_differ.append(f"opening balance stored {statement.opening_balance}, read {parsed.opening_balance}")
    if statement.closing_balance is not None and parsed.closing_balance is not None \
            and Decimal(statement.closing_balance) != parsed.closing_balance:
        plan.totals_differ.append(f"closing balance stored {statement.closing_balance}, read {parsed.closing_balance}")
    return plan


def apply_plan(session: Session, statement: Statement, plan: ReimportPlan, institution: str,
               user_rules: list) -> None:
    if not plan.reconciles:
        raise ReimportRefused("the new reading does not reconcile, so nothing was changed")
    for s, description in plan.fill_descriptions:
        s.description = description
    for s, _old, new in plan.move_dates:
        s.booked_on = new
    for read in plan.to_add:
        session.add(new_transaction(statement.id, statement.account_id, institution, read, user_rules))
    session.commit()


def describe(plan: ReimportPlan, limit: int = 50) -> dict:
    """What the person would see: counts, and up to `limit` examples of each kind of change."""
    def stored(t: Transaction) -> dict:
        return {"id": t.id, "booked_on": t.booked_on.isoformat(), "amount": str(t.amount),
                "description": t.description}

    def read(r: ReadTransaction) -> dict:
        return {"booked_on": r.booked_on.isoformat(), "amount": str(r.amount), "description": r.description}

    return {
        "reconciles": plan.reconciles,
        "matched": plan.matched,
        "counts": {
            "fill_descriptions": len(plan.fill_descriptions),
            "move_dates": len(plan.move_dates),
            "to_add": len(plan.to_add),
            "differs": len(plan.differs),
            "to_review": len(plan.to_review),
        },
        "fill_descriptions": [{**stored(t), "new_description": d} for t, d in plan.fill_descriptions[:limit]],
        "move_dates": [{**stored(t), "new_booked_on": new.isoformat()} for t, _old, new in plan.move_dates[:limit]],
        "to_add": [read(r) for r in plan.to_add[:limit]],
        "differs": [{**stored(t), "read": read(r)} for t, r in plan.differs[:limit]],
        "to_review": [stored(t) for t in plan.to_review[:limit]],
        "totals_differ": plan.totals_differ,
    }


def reimport_from_pdf(session: Session, statement: Statement, pdf_bytes: bytes,
                      password_candidates: list[str], user_rules: list, work_dir: Path,
                      apply: bool) -> dict:
    """Read the stored PDF again with the current parsers, and apply the plan only if asked.

    The summary is built before anything is written, because committing expires the
    stored objects it describes.
    """
    account = session.get(Account, statement.account_id)
    text, password_used = extract_text(pdf_bytes, work_dir, password_candidates)
    parser = find_parser(text)
    if parser.institution != account.institution:
        raise ReimportRefused(
            f"this PDF is read as {parser.institution}, but the statement belongs to a {account.institution} account"
        )
    text = repair_text(parser, text, pdf_bytes, work_dir, password_used)
    parsed = parser.parse(text)
    result = reconcile(parsed)
    plan = plan_reimport(session, statement, parsed, result.ok)
    summary = describe(plan)
    summary["statement_id"] = statement.id
    summary["applied"] = False
    if apply:
        apply_plan(session, statement, plan, parser.institution, user_rules)
        summary["applied"] = True
    return summary
