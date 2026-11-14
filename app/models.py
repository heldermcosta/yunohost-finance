from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint
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
    # One term per line. Removed from descriptions before merchants are compared.
    ignored_terms: Mapped[str] = mapped_column(Text, default="")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    institution: Mapped[str] = mapped_column(String(50))   # matches a parser's institution id
    label: Mapped[str] = mapped_column(String(120))
    account_ref_last4: Mapped[str] = mapped_column(String(4), default="")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    # Household accounts are visible to every user whose view scope is "all".
    household: Mapped[bool] = mapped_column(Boolean, default=False)


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
    # "default": set by built-in keywords. "rule": set by a saved rule. "manual": set by a person.
    category_source: Mapped[str] = mapped_column(String(10), default="default")
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # What the transaction is about: a house, a car, a person. Free text, per owner's choice.
    object_name: Mapped[str | None] = mapped_column(String(60), nullable=True)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)


class Rule(Base):
    """If a description contains `contains` (any case), set the category. Per owner."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    contains: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60))
    object_name: Mapped[str | None] = mapped_column(String(60), nullable=True)


class PortfolioSnapshot(Base):
    """Totals of an investment report at its date: what is held, not spending."""

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id", ondelete="CASCADE"), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    period_end: Mapped[date] = mapped_column(Date)
    total_assets: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    cash: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    term_deposits: Mapped[str] = mapped_column(Text, default="[]")  # JSON list


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("owner_id", "category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(60))
    monthly_limit: Mapped[Decimal] = mapped_column(Numeric(14, 2))


Index("ix_transactions_account_booked", Transaction.account_id, Transaction.booked_on)


class Bill(Base):
    """A utility bill (electricity, gas, water). Kept apart from bank transactions: bills are not
    linked to payments and do not change the bank totals or categories."""

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    utility: Mapped[str] = mapped_column(String(15))            # electricity | gas | water
    provider: Mapped[str] = mapped_column(String(60), default="")
    location: Mapped[str] = mapped_column(String(200), default="")  # supply address as printed
    invoice: Mapped[str] = mapped_column(String(60), default="")
    issued: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)  # the bill counts in this month
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))     # what the bill asks for, positive
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | review
    source_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class BillHouse(Base):
    """A house and an address keyword. A bill goes to the first house whose keyword appears in its
    supply address; other addresses stay unassigned until a keyword is added for them."""

    __tablename__ = "bill_houses"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    keyword: Mapped[str] = mapped_column(String(120))
