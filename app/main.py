"""HTTP API and the web page. Every endpoint needs a user: on YunoHost that comes from SSO
(the Authorization header SSOwat adds), in development from FINANCE_DEV_USER."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, sessionmaker

from . import access, crypto, exports
from .auth import username_from_authorization
from .categories import DEFAULT_CATEGORIES, UNCATEGORISED
from .config import Settings
from .db import setup_schema
from .ingest import (AccountNeeded, DuplicateStatement, PasswordRequired, WrongAccount,
                     WrongInstitution, import_pdf, read_pdf)
from .models import Account, AccountShare, Budget, Category, PdfPassword, Rule, Statement, Transaction, User
from .parsers import NoParserError
from .transfers import detect_transfers, link, unlink

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
STATIC_DIR = Path(__file__).resolve().parent / "static"
PDF_PASSWORD_AAD = b"pdf-password"
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


class AccountIn(BaseModel):
    institution: str = Field(min_length=1, max_length=50)
    label: str = Field(min_length=1, max_length=120)
    account_ref_last4: str = Field(default="", max_length=4)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    household: bool = False


class AccountPatch(BaseModel):
    institution: str | None = Field(default=None, min_length=1, max_length=50)
    label: str | None = Field(default=None, min_length=1, max_length=120)
    account_ref_last4: str | None = Field(default=None, max_length=4)
    household: bool | None = None


class ShareIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)


class ScopeIn(BaseModel):
    view_scope: str


class PdfPasswordIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class TransactionPatch(BaseModel):
    category: str | None = Field(default=None, min_length=1, max_length=60)
    is_transfer: bool | None = None
    description: str | None = Field(default=None, min_length=1, max_length=255)


class BulkCategoryIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=2000)
    category: str = Field(min_length=1, max_length=60)
    save_rule: str | None = Field(default=None, min_length=2, max_length=120)


class RuleIn(BaseModel):
    contains: str = Field(min_length=2, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    apply: bool = True


class BulkByTextIn(BaseModel):
    text: str = Field(min_length=2, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    save_rule: bool = False


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class BudgetIn(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    monthly_limit: Decimal = Field(gt=0, max_digits=14, decimal_places=2)


def _err(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _description_key(description: str) -> str:
    words = [w for w in re.sub(r"[\d/\\.*#-]+", " ", description.upper()).split() if len(w) > 2]
    return " ".join(words[:2])


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, connect_args=connect_args)
    setup_schema(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    key = crypto.load_or_create_key(settings.key_file)

    with SessionLocal() as s:
        if s.query(Category).count() == 0:
            for name in DEFAULT_CATEGORIES:
                s.add(Category(name=name))
            s.commit()

    app = FastAPI(title="yunohost-finance", version="0.3.0")
    app.state.settings = settings

    def get_session():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
        username = username_from_authorization(request.headers.get("Authorization")) or settings.dev_user
        if not username:
            raise HTTPException(status_code=401, detail="not signed in")
        return access.get_or_create_user(session, username)

    def visible_ids(session: Session, user: User) -> list[int]:
        return [a.id for a in access.visible_accounts(session, user)]

    def account_for_read(session: Session, user: User, account_id: int) -> Account:
        account = session.get(Account, account_id)
        if account is None or not access.can_see(session, user, account):
            raise HTTPException(status_code=404, detail="account not found")
        return account

    def account_for_owner(session: Session, user: User, account_id: int) -> Account:
        account = session.get(Account, account_id)
        if account is None or not access.is_owner(user, account):
            raise HTTPException(status_code=404, detail="account not found")
        return account

    def saved_passwords(session: Session, user: User) -> list[str]:
        rows = session.query(PdfPassword).filter_by(owner_id=user.id).order_by(PdfPassword.id).all()
        return [crypto.decrypt(key, r.sealed, PDF_PASSWORD_AAD).decode() for r in rows]

    def user_rules(session: Session, user: User) -> list[tuple[str, str]]:
        rows = session.query(Rule).filter_by(owner_id=user.id).order_by(Rule.id.desc()).all()
        return [(r.contains, r.category) for r in rows]

    def ensure_category(session: Session, name: str) -> None:
        if session.query(Category).filter_by(name=name).first() is None:
            session.add(Category(name=name))

    def accepted_query(session: Session, user: User):
        return (session.query(Transaction)
                .join(Statement, Statement.id == Transaction.statement_id)
                .filter(Transaction.account_id.in_(visible_ids(session, user)), Statement.status == "accepted"))

    def serialise(t: Transaction, accounts: dict[int, str]) -> dict:
        return {"id": t.id, "account_id": t.account_id, "account": accounts.get(t.account_id, ""),
                "booked_on": t.booked_on.isoformat(), "description": t.description,
                "amount": str(t.amount), "balance": str(t.balance), "category": t.category,
                "category_source": t.category_source, "is_transfer": t.is_transfer,
                "transfer_id": t.transfer_id}

    def account_labels(session: Session, user: User) -> dict[int, str]:
        return {a.id: a.label for a in access.visible_accounts(session, user)}

    def export_rows(session: Session, user: User) -> list[dict]:
        labels = account_labels(session, user)
        rows = accepted_query(session, user).order_by(Transaction.booked_on, Transaction.id).all()
        return [serialise(t, labels) for t in rows]

    # ---- basics -----------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def web_app() -> FileResponse:
        # The page itself is public; every API call it makes is still checked by SSO.
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/api/me")
    def me(user: User = Depends(get_current_user)) -> dict:
        return {"username": user.username, "view_scope": user.view_scope}

    @app.put("/api/me/scope")
    def set_scope(body: ScopeIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        if body.view_scope not in access.VIEW_SCOPES:
            raise HTTPException(status_code=422, detail="view_scope must be own_shared or all")
        user = session.merge(user)
        user.view_scope = body.view_scope
        session.commit()
        return {"view_scope": user.view_scope}

    # ---- accounts ---------------------------------------------------------------

    @app.get("/api/accounts")
    def list_accounts(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        owners = {u.id: u.username for u in session.query(User).all()}
        return [
            {"id": a.id, "institution": a.institution, "label": a.label,
             "account_ref_last4": a.account_ref_last4, "currency": a.currency,
             "household": a.household, "owner": owners.get(a.owner_id, ""),
             "is_owner": a.owner_id == user.id,
             "can_edit_categories": access.can_access(session, user, a)}
            for a in access.visible_accounts(session, user)
        ]

    @app.post("/api/accounts", status_code=201)
    def create_account(body: AccountIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        account = Account(owner_id=user.id, institution=body.institution.strip().lower(), label=body.label,
                          account_ref_last4=body.account_ref_last4.strip(), currency=body.currency.upper(),
                          household=body.household)
        session.add(account)
        session.commit()
        return {"id": account.id}

    @app.patch("/api/accounts/{account_id}")
    def update_account(account_id: int, body: AccountPatch, user: User = Depends(get_current_user),
                       session: Session = Depends(get_session)) -> dict:
        account = account_for_owner(session, user, account_id)
        if body.institution is not None:
            account.institution = body.institution.strip().lower()
        if body.label is not None:
            account.label = body.label
        if body.account_ref_last4 is not None:
            account.account_ref_last4 = body.account_ref_last4.strip()
        if body.household is not None:
            account.household = body.household
        session.commit()
        return {"id": account.id}

    @app.delete("/api/accounts/{account_id}")
    def delete_account(account_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        account = account_for_owner(session, user, account_id)
        if session.query(Statement).filter_by(account_id=account.id).first() is not None:
            raise _err(409, "has_statements", "This account has statements. Delete them first.")
        session.query(AccountShare).filter_by(account_id=account.id).delete()
        session.delete(account)
        session.commit()
        return {"deleted": account_id}

    @app.post("/api/accounts/{account_id}/shares", status_code=201)
    def share_account(account_id: int, body: ShareIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        account = account_for_owner(session, user, account_id)
        other = access.get_or_create_user(session, body.username)
        if session.get(AccountShare, (account.id, other.id)) is None:
            session.add(AccountShare(account_id=account.id, user_id=other.id))
            session.commit()
        return {"shared_with": other.username}

    @app.delete("/api/accounts/{account_id}/shares/{username}")
    def unshare_account(account_id: int, username: str, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        account = account_for_owner(session, user, account_id)
        other = session.query(User).filter_by(username=username).first()
        if other is not None:
            session.query(AccountShare).filter_by(account_id=account.id, user_id=other.id).delete()
            session.commit()
        return {"unshared": username}

    # ---- statements -------------------------------------------------------------

    @app.post("/api/statements", status_code=201)
    async def upload_statement(
        file: UploadFile = File(...),
        account_id: int | None = Form(None),
        pdf_password: str | None = Form(None),
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> dict:
        account = None
        if account_id is not None:
            account = account_for_owner(session, user, account_id)

        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise _err(413, "too_large", "File is larger than 20 MB.")
        if not data.startswith(b"%PDF"):
            raise _err(422, "not_pdf", "This is not a PDF file.")

        owner_accounts = session.query(Account).filter_by(owner_id=user.id).order_by(Account.id).all()
        # The password sent with this upload is tried first, then the saved ones.
        candidates = ([pdf_password] if pdf_password else []) + saved_passwords(session, user)
        try:
            result = import_pdf(
                session,
                pdf_bytes=data,
                original_name=file.filename or "statement.pdf",
                account=account,
                owner_accounts=owner_accounts,
                uploaded_by_id=user.id,
                password_candidates=candidates,
                user_rules=user_rules(session, user),
                pdf_dir=settings.pdf_dir,
                work_dir=settings.work_dir,
            )
        except DuplicateStatement as exc:
            raise _err(409, "duplicate", "This statement was already imported.", statement_id=exc.statement_id)
        except WrongInstitution as exc:
            raise _err(422, "wrong_institution", str(exc))
        except WrongAccount as exc:
            raise _err(422, "wrong_account", str(exc))
        except NoParserError:
            raise _err(422, "no_parser", "No parser recognises this statement yet.")
        except AccountNeeded as exc:
            options = [{"id": a.id, "label": a.label, "account_ref_last4": a.account_ref_last4}
                       for a in owner_accounts if a.id in exc.candidates]
            if options:
                message = f"Several {exc.institution} accounts match. Choose one."
            else:
                message = f"No {exc.institution} account matches this statement. Create one first."
            raise _err(422, "account_needed", message, accounts=options, suggested=exc.suggested)
        except PasswordRequired:
            raise _err(422, "password_required", "This PDF is password protected.")

        reply = asdict(result)
        used = reply.pop("pdf_password")
        if used is not None and used not in candidates[1:]:
            # A new password that worked is kept for future statements.
            session.add(PdfPassword(owner_id=user.id, label=f"from {file.filename or 'statement'}"[:120],
                                    sealed=crypto.encrypt(key, used.encode(), PDF_PASSWORD_AAD)))
            session.commit()
        if reply["status"] == "accepted":
            detect_transfers(session, visible_ids(session, user))
        return reply

    @app.get("/api/review")
    def review_queue(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        labels = account_labels(session, user)
        rows = (session.query(Statement).filter(Statement.account_id.in_(visible_ids(session, user)),
                                                Statement.status == "review")
                .order_by(Statement.period_start).all())
        return [
            {"id": s.id, "account_id": s.account_id, "account": labels.get(s.account_id, ""),
             "original_name": s.original_name,
             "period": [s.period_start.isoformat(), s.period_end.isoformat()],
             "reconciliation": s.reconciliation, "notes": s.notes.split("\n") if s.notes else []}
            for s in rows
        ]

    @app.post("/api/statements/{statement_id}/accept")
    def accept_statement(statement_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")
        account = account_for_read(session, user, statement.account_id)
        if not access.is_owner(user, account):
            raise HTTPException(status_code=403, detail="only the owner can accept a statement")
        statement.status = "accepted"
        session.commit()
        detect_transfers(session, visible_ids(session, user))
        return {"id": statement.id, "status": statement.status}

    @app.get("/api/statements")
    def list_statements(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        ids = visible_ids(session, user)
        labels = account_labels(session, user)
        owners = {a.id: access.is_owner(user, a) for a in session.query(Account).filter(Account.id.in_(ids))}
        counts = dict(session.query(Transaction.statement_id, func.count(Transaction.id))
                      .filter(Transaction.account_id.in_(ids)).group_by(Transaction.statement_id).all())
        rows = (session.query(Statement).filter(Statement.account_id.in_(ids))
                .order_by(Statement.period_start.desc(), Statement.id.desc()).all())
        return [
            {"id": s.id, "account_id": s.account_id, "account": labels.get(s.account_id, ""),
             "original_name": s.original_name, "status": s.status, "reconciliation": s.reconciliation,
             "period": [s.period_start.isoformat(), s.period_end.isoformat()],
             "transactions": counts.get(s.id, 0), "can_delete": owners.get(s.account_id, False)}
            for s in rows
        ]

    @app.delete("/api/statements/{statement_id}")
    def reject_statement(statement_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        """Delete a statement, its transactions and its PDF. Owner only, accepted or not.

        Transfer links to transactions in other accounts are removed first, so the
        partner is no longer counted as a transfer.
        """
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")
        account = account_for_read(session, user, statement.account_id)
        if not access.is_owner(user, account):
            raise HTTPException(status_code=403, detail="only the owner can delete a statement")
        pdf_path = settings.pdf_dir / statement.pdf_file
        for t in session.query(Transaction).filter_by(statement_id=statement.id).all():
            if t.is_transfer:
                unlink(t, session)
            session.delete(t)
        session.delete(statement)
        session.commit()
        pdf_path.unlink(missing_ok=True)
        return {"deleted": statement_id}

    @app.get("/api/statements/{statement_id}/pdf")
    def statement_pdf(statement_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> Response:
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")
        account_for_read(session, user, statement.account_id)
        data = read_pdf(statement, key, settings.pdf_dir)
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{statement.original_name}"'})

    # ---- transactions -----------------------------------------------------------

    @app.get("/api/transactions")
    def list_transactions(
        account_id: int | None = None, category: str | None = None,
        start: date | None = None, end: date | None = None, q: str | None = None,
        transfers: str = "include", limit: int = 200, offset: int = 0,
        user: User = Depends(get_current_user), session: Session = Depends(get_session),
    ) -> dict:
        query = accepted_query(session, user)
        if account_id is not None:
            query = query.filter(Transaction.account_id == account_id)
        if category:
            query = query.filter(Transaction.category == category)
        if start:
            query = query.filter(Transaction.booked_on >= start)
        if end:
            query = query.filter(Transaction.booked_on <= end)
        if q:
            query = query.filter(func.upper(Transaction.description).contains(q.upper()))
        if transfers == "exclude":
            query = query.filter(Transaction.is_transfer.is_(False))
        elif transfers == "only":
            query = query.filter(Transaction.is_transfer.is_(True))
        total = query.count()
        rows = query.order_by(Transaction.booked_on.desc(), Transaction.id.desc()).offset(offset).limit(min(limit, 1000)).all()
        labels = account_labels(session, user)
        return {"total": total, "items": [serialise(t, labels) for t in rows]}

    @app.get("/api/transactions/{transaction_id}/similar")
    def similar_transactions(transaction_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        t = session.get(Transaction, transaction_id)
        if t is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        account_for_read(session, user, t.account_id)
        key_text = _description_key(t.description)
        if not key_text:
            return []
        rows = (accepted_query(session, user)
                .filter(Transaction.id != t.id, func.upper(Transaction.description).contains(key_text))
                .order_by(Transaction.booked_on.desc()).limit(100).all())
        labels = account_labels(session, user)
        return [serialise(r, labels) for r in rows]

    def set_category(session: Session, user: User, t: Transaction, category: str) -> None:
        account = session.get(Account, t.account_id)
        if account is None or not access.can_access(session, user, account):
            raise HTTPException(status_code=404, detail="transaction not found")
        ensure_category(session, category)
        t.category = category
        t.category_source = "manual"

    @app.patch("/api/transactions/{transaction_id}")
    def update_transaction(transaction_id: int, body: TransactionPatch, user: User = Depends(get_current_user),
                           session: Session = Depends(get_session)) -> dict:
        t = session.get(Transaction, transaction_id)
        if t is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        account = session.get(Account, t.account_id)
        if account is None or not access.can_access(session, user, account):
            raise HTTPException(status_code=404, detail="transaction not found")
        if body.category is not None:
            set_category(session, user, t, body.category)
        if body.description is not None:
            t.description = body.description
        if body.is_transfer is False and t.is_transfer:
            unlink(t, session)
        elif body.is_transfer is True:
            t.is_transfer = True
        session.commit()
        return serialise(t, account_labels(session, user))

    @app.post("/api/transactions/bulk-category")
    def bulk_category(body: BulkCategoryIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        updated = 0
        for t in session.query(Transaction).filter(Transaction.id.in_(body.ids)).all():
            account = session.get(Account, t.account_id)
            if account is not None and access.can_access(session, user, account):
                set_category(session, user, t, body.category)
                updated += 1
        rule_id = None
        if body.save_rule:
            rule_id = _upsert_rule(session, user, body.save_rule, body.category)
        session.commit()
        return {"updated": updated, "rule_id": rule_id}

    @app.post("/api/transactions/bulk-category-by-text")
    def bulk_category_by_text(body: BulkByTextIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        text = body.text.strip()
        rows = accepted_query(session, user).filter(func.upper(Transaction.description).contains(text.upper())).all()
        updated = 0
        for t in rows:
            account = session.get(Account, t.account_id)
            if account is not None and access.can_access(session, user, account):
                set_category(session, user, t, body.category)
                updated += 1
        rule_id = _upsert_rule(session, user, text, body.category) if body.save_rule else None
        session.commit()
        return {"updated": updated, "rule_id": rule_id}

    @app.get("/api/summary/monthly")
    def monthly_summary(year: int | None = None, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        query = accepted_query(session, user).filter(Transaction.is_transfer.is_(False))
        if year:
            query = query.filter(Transaction.booked_on >= date(year, 1, 1), Transaction.booked_on <= date(year, 12, 31))
        totals: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: {"income": Decimal("0"), "expenses": Decimal("0")})
        for t in query.all():
            bucket = "income" if t.amount > 0 else "expenses"
            totals[(t.booked_on.strftime("%Y-%m"), t.category)][bucket] += abs(t.amount)
        return [{"month": m, "category": c, "income": str(v["income"]), "expenses": str(v["expenses"])}
                for (m, c), v in sorted(totals.items())]

    @app.get("/api/summary/years")
    def summary_years(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[int]:
        rows = accepted_query(session, user).with_entities(Transaction.booked_on).distinct().all()
        return sorted({r[0].year for r in rows}, reverse=True)

    # ---- categories and rules ---------------------------------------------------

    @app.get("/api/categories")
    def list_categories(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        names = {c.name: c.id for c in session.query(Category).all()}
        used = {r[0] for r in accepted_query(session, user).with_entities(Transaction.category).distinct().all()}
        for name in sorted(used - set(names)):
            names[name] = None
        return [{"id": cid, "name": name} for name, cid in sorted(names.items())]

    @app.post("/api/categories", status_code=201)
    def add_category(body: CategoryIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        name = body.name.strip()
        if session.query(Category).filter_by(name=name).first() is None:
            session.add(Category(name=name))
            session.commit()
        return {"name": name}

    @app.patch("/api/categories/{category_id}")
    def rename_category(category_id: int, body: CategoryIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        cat = session.get(Category, category_id)
        if cat is None:
            raise HTTPException(status_code=404, detail="category not found")
        new = body.name.strip()
        old = cat.name
        cat.name = new
        for t in accepted_query(session, user).filter(Transaction.category == old).all():
            t.category = new
        for rule in session.query(Rule).filter_by(category=old).all():
            rule.category = new
        for budget in session.query(Budget).filter_by(category=old).all():
            budget.category = new
        session.commit()
        return {"name": new}

    @app.delete("/api/categories/{category_id}")
    def delete_category(category_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        cat = session.get(Category, category_id)
        if cat is None:
            raise HTTPException(status_code=404, detail="category not found")
        for t in accepted_query(session, user).filter(Transaction.category == cat.name).all():
            t.category = UNCATEGORISED
            t.category_source = "manual"
        session.query(Rule).filter_by(category=cat.name).delete()
        session.query(Budget).filter_by(category=cat.name).delete()
        session.delete(cat)
        session.commit()
        return {"deleted": category_id}

    def _upsert_rule(session: Session, user: User, contains: str, category: str) -> int:
        ensure_category(session, category)
        existing = session.query(Rule).filter(Rule.owner_id == user.id,
                                              func.upper(Rule.contains) == contains.upper()).first()
        if existing is not None:
            existing.category = category
            return existing.id
        rule = Rule(owner_id=user.id, contains=contains, category=category)
        session.add(rule)
        session.flush()
        return rule.id

    @app.get("/api/rules")
    def list_rules(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        rows = session.query(Rule).filter_by(owner_id=user.id).order_by(Rule.id).all()
        return [{"id": r.id, "contains": r.contains, "category": r.category} for r in rows]

    @app.post("/api/rules", status_code=201)
    def add_rule(body: RuleIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        rule_id = _upsert_rule(session, user, body.contains.strip(), body.category.strip())
        updated = 0
        if body.apply:
            needle = body.contains.strip().upper()
            for t in accepted_query(session, user).filter(func.upper(Transaction.description).contains(needle)).all():
                account = session.get(Account, t.account_id)
                if account is not None and access.can_access(session, user, account) and t.category_source != "manual":
                    t.category = body.category.strip()
                    t.category_source = "rule"
                    updated += 1
        session.commit()
        return {"id": rule_id, "updated": updated}

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        rule = session.get(Rule, rule_id)
        if rule is None or rule.owner_id != user.id:
            raise HTTPException(status_code=404, detail="rule not found")
        session.delete(rule)
        session.commit()
        return {"deleted": rule_id}

    # ---- budgets ----------------------------------------------------------------

    @app.get("/api/budgets")
    def list_budgets(month: str | None = None, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        if month is None:
            month = date.today().strftime("%Y-%m")
        if not MONTH_RE.match(month):
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        year, mon = int(month[:4]), int(month[5:])
        start = date(year, mon, 1)
        end = date(year + (mon == 12), mon % 12 + 1, 1)
        spent: dict[str, Decimal] = defaultdict(Decimal)
        for t in (accepted_query(session, user)
                  .filter(Transaction.is_transfer.is_(False), Transaction.amount < 0,
                          Transaction.booked_on >= start, Transaction.booked_on < end).all()):
            spent[t.category] += -t.amount
        out = []
        for b in session.query(Budget).filter_by(owner_id=user.id).order_by(Budget.category).all():
            used = spent.get(b.category, Decimal("0"))
            percent = float(used / b.monthly_limit * 100) if b.monthly_limit else 0.0
            status = "over" if percent >= 100 else "near" if percent >= 80 else "ok"
            out.append({"id": b.id, "category": b.category, "monthly_limit": str(b.monthly_limit),
                        "spent": str(used), "remaining": str(b.monthly_limit - used),
                        "percent": round(percent, 1), "status": status, "month": month})
        return out

    @app.put("/api/budgets")
    def set_budget(body: BudgetIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        budget = session.query(Budget).filter_by(owner_id=user.id, category=body.category).first()
        if budget is None:
            budget = Budget(owner_id=user.id, category=body.category, monthly_limit=body.monthly_limit)
            session.add(budget)
        else:
            budget.monthly_limit = body.monthly_limit
        ensure_category(session, body.category)
        session.commit()
        return {"id": budget.id, "category": budget.category, "monthly_limit": str(budget.monthly_limit)}

    @app.delete("/api/budgets/{budget_id}")
    def delete_budget(budget_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        budget = session.get(Budget, budget_id)
        if budget is None or budget.owner_id != user.id:
            raise HTTPException(status_code=404, detail="budget not found")
        session.delete(budget)
        session.commit()
        return {"deleted": budget_id}

    # ---- transfers --------------------------------------------------------------

    @app.post("/api/transfers/detect")
    def run_transfer_detection(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        return {"pairs": detect_transfers(session, visible_ids(session, user))}

    # ---- saved PDF passwords ----------------------------------------------------

    @app.get("/api/pdf-passwords")
    def list_pdf_passwords(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        rows = session.query(PdfPassword).filter_by(owner_id=user.id).order_by(PdfPassword.id).all()
        # Labels only. Passwords are never returned.
        return [{"id": r.id, "label": r.label, "created_at": r.created_at.isoformat()} for r in rows]

    @app.post("/api/pdf-passwords", status_code=201)
    def add_pdf_password(body: PdfPasswordIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        row = PdfPassword(owner_id=user.id, label=body.label,
                          sealed=crypto.encrypt(key, body.password.encode(), PDF_PASSWORD_AAD))
        session.add(row)
        session.commit()
        return {"id": row.id, "label": row.label}

    @app.delete("/api/pdf-passwords/{password_id}")
    def delete_pdf_password(password_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        row = session.get(PdfPassword, password_id)
        if row is None or row.owner_id != user.id:
            raise HTTPException(status_code=404, detail="password not found")
        session.delete(row)
        session.commit()
        return {"deleted": password_id}

    # ---- exports ----------------------------------------------------------------

    def download(content: bytes, media_type: str, filename: str) -> Response:
        return Response(content=content, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/export/transactions.csv")
    def export_csv(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> Response:
        return download(exports.transactions_csv(export_rows(session, user)), "text/csv", "transactions.csv")

    @app.get("/api/export/transactions.xlsx")
    def export_xlsx(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> Response:
        return download(exports.transactions_xlsx(export_rows(session, user)),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "transactions.xlsx")

    @app.get("/api/export/report.pdf")
    def export_report(year: int = date.today().year, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> Response:
        return download(exports.report_pdf(export_rows(session, user), str(year)), "application/pdf", f"report-{year}.pdf")

    @app.get("/api/export/backup.json")
    def export_backup(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> Response:
        visible = visible_ids(session, user)
        accounts = [{"id": a.id, "institution": a.institution, "label": a.label,
                     "account_ref_last4": a.account_ref_last4, "currency": a.currency, "household": a.household}
                    for a in access.visible_accounts(session, user)]
        statements = [{"id": s.id, "account_id": s.account_id, "original_name": s.original_name,
                       "period_start": s.period_start, "period_end": s.period_end, "status": s.status,
                       "reconciliation": s.reconciliation}
                      for s in session.query(Statement).filter(Statement.account_id.in_(visible)).all()]
        payload = {
            "accounts": accounts,
            "statements": statements,
            "transactions": [t for t in export_rows(session, user)],
            "categories": [c.name for c in session.query(Category).order_by(Category.name).all()],
            "rules": [{"contains": r.contains, "category": r.category} for r in session.query(Rule).filter_by(owner_id=user.id).all()],
            "budgets": [{"category": b.category, "monthly_limit": str(b.monthly_limit)} for b in session.query(Budget).filter_by(owner_id=user.id).all()],
        }
        return download(exports.backup_json(payload), "application/json", "finance-backup.json")

    return app
