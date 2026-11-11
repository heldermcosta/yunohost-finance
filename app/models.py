from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, LargeBinary, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    # "own_shared": accounts I own or that were shared with me.
    # "all": every account I can access. See README: this matches "own_shared" today.
    view_scope: Mapped[str] = mapped_column(String(20), default="own_shared")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    institution: Mapped[str] = mapped_column(String(50))   # matches a parser's institution id
    label: Mapped[str] = mapped_column(String(120))
    account_ref_last4: Mapped[str] = mapped_column(String(4), default="")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")


class AccountShare(Base):
    __tablename__ = "account_shares"

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)


class Statement(Base):
    __tablename__ = "statements"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    original_name: Mapped[str] = mapped_column(String(255))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    closing_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    # "accepted": counts in analytics. "review": waiting for a person to confirm.
    status: Mapped[str] = mapped_column(String(20), default="review")
    reconciliation: Mapped[str] = mapped_column(String(10))  # ok | warning | failed
    notes: Mapped[str] = mapped_column(Text, default="")
    pdf_file: Mapped[str] = mapped_column(String(80))        # name inside the encrypted pdf dir
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class PdfPassword(Base):
    """A password that opens some of the owner's PDFs. Stored encrypted with the server key."""

    __tablename__ = "pdf_passwords"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    sealed: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    booked_on: Mapped[date] = mapped_column(Date)
    value_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))  # signed: credit > 0, debit < 0
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    category: Mapped[str] = mapped_column(String(60), default="Uncategorised")


Index("ix_transactions_account_booked", Transaction.account_id, Transaction.booked_on)
