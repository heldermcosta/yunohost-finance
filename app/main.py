"""HTTP API and the web page. Every endpoint needs a user: on YunoHost that comes from SSO
(the Authorization header SSOwat adds), in development from FINANCE_DEV_USER."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.responses import JSONResponse
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, sessionmaker

from . import access, crypto, exports
from .auth import username_from_authorization
from .categories import DEFAULT_CATEGORIES, SEPARATOR, UNCATEGORISED, in_category, parent_of
from .config import Settings
from .db import setup_schema
from .ingest import (AccountNeeded, DuplicateStatement, PasswordRequired, SamePeriodStatement, WrongAccount,
                     WrongInstitution, import_pdf, read_pdf)
from .models import (Account, AccountShare, Budget, Category, PdfPassword, PortfolioSnapshot, Rule, Statement,
                     Transaction, User)
from .parsers import NoParserError
from .reimport import ReimportRefused, reimport_from_pdf
from .transfers import detect_transfers, link, unlink

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
STATIC_DIR = Path(__file__).resolve().parent / "static"
PDF_PASSWORD_AAD = b"pdf-password"
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _valid_month(month: str) -> bool:
    return bool(MONTH_RE.match(month)) and 1 <= int(month[5:]) <= 12


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


class IgnoredTermsIn(BaseModel):
    terms: list[str] = Field(default_factory=list, max_length=50)


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
    object_name: str | None = Field(default=None, max_length=60)


class BulkCategoryIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=2000)
    category: str = Field(min_length=1, max_length=60)
    save_rule: str | None = Field(default=None, min_length=2, max_length=120)
    # None leaves the object unchanged; an empty string clears it.
    object_name: str | None = Field(default=None, max_length=60)


class RuleIn(BaseModel):
    contains: str = Field(min_length=2, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    apply: bool = True


class BulkByTextIn(BaseModel):
    text: str = Field(min_length=2, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    save_rule: bool = False
    object_name: str | None = Field(default=None, max_length=60)


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class BudgetIn(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    monthly_limit: Decimal = Field(gt=0, max_digits=14, decimal_places=2)


def _err(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


# Institutions whose statements show what is owed, not a bank balance.
CARD_INSTITUTIONS = {"affinity", "wizink", "cofidis", "universo", "cetelem"}


def _shift_month(month: str, offset: int) -> str:
    index = int(month[:4]) * 12 + (int(month[5:]) - 1) + offset
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _ignored_terms(user: User) -> tuple[str, ...]:
    """The user's terms to ignore, normalised: upper case, single spaces, no duplicates."""
    lines = (user.ignored_terms or "").splitlines()
    return tuple(dict.fromkeys(" ".join(line.upper().split()) for line in lines if line.strip()))


def _description_key(description: str, ignored: tuple[str, ...] = ()) -> str:
    """Merchant key: the first two meaningful words once ignored terms are removed.

    Bank prefixes such as "PAG SERV" are removed first, so the merchant that follows
    is what gets compared.
    """
    text = " ".join(description.upper().split())
    for term in sorted(ignored, key=len, reverse=True):
        text = re.sub(r"(?<![A-Z0-9])" + re.escape(term) + r"(?![A-Z0-9])", " ", text)
    words = [w for w in re.sub(r"[\d/\\.*#-]+", " ", text).split() if len(w) > 2]
    return " ".join(words[:2])


def _delete_statement(session: Session, statement: Statement, pdf_dir: Path) -> None:
    """Remove a statement, its transactions and its PDF. Transfer links are unlinked first."""
    pdf_path = pdf_dir / statement.pdf_file
    for t in session.query(Transaction).filter_by(statement_id=statement.id).all():
        if t.is_transfer:
            unlink(t, session)
        session.delete(t)
    session.query(PortfolioSnapshot).filter_by(statement_id=statement.id).delete()
    session.delete(statement)
    session.commit()
    pdf_path.unlink(missing_ok=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, connect_args=connect_args)
    setup_schema(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    key = crypto.load_or_create_key(settings.key_file)

    with SessionLocal() as s:
        # Add the default categories once. Installs that predate subcategories get
        # the full default tree here. After that, nothing is added back.
        has_subcategories = s.query(Category).filter(Category.name.contains(SEPARATOR)).first() is not None
        if not has_subcategories:
            existing = {c.name for c in s.query(Category).all()}
            for name in DEFAULT_CATEGORIES:
                if name not in existing:
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
        return [(r.contains, r.category, r.object_name) for r in rows]

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
                "transfer_id": t.transfer_id, "object_name": t.object_name, "statement_id": t.statement_id}

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
        return {"username": user.username, "view_scope": user.view_scope,
                "ignored_terms": list(_ignored_terms(user))}

    @app.put("/api/me/ignored-terms")
    def set_ignored_terms(body: IgnoredTermsIn, user: User = Depends(get_current_user),
                          session: Session = Depends(get_session)) -> dict:
        terms = list(dict.fromkeys(" ".join(t.upper().split()) for t in body.terms if t.strip()))
        if any(len(t) > 40 for t in terms):
            raise HTTPException(status_code=422, detail="each term must be 40 characters or fewer")
        user = session.merge(user)
        user.ignored_terms = "\n".join(terms)
        session.commit()
        return {"ignored_terms": terms}

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
        except (DuplicateStatement, SamePeriodStatement) as exc:
            # The file is already stored (or is another copy of a stored statement): read it
            # again and bring in whatever is new, instead of refusing it.
            return JSONResponse(refresh_statement(session, user, exc.statement_id, data, candidates),
                                status_code=200)
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

    def refresh_statement(session: Session, user: User, statement_id: int, data: bytes, candidates: list[str]) -> dict:
        statement = session.get(Statement, statement_id)
        account = account_for_read(session, user, statement.account_id)
        if not access.is_owner(user, account):
            raise _err(409, "duplicate", "This statement was already imported.", statement_id=statement_id)
        try:
            summary = reimport_from_pdf(session, statement, data, candidates, user_rules(session, user),
                                        settings.work_dir, apply=True)
        except ReimportRefused as exc:
            raise _err(409, "duplicate", f"This statement was already imported, and the file cannot be "
                       f"read as the same statement: {exc}", statement_id=statement_id)
        counts = summary["counts"]
        changed = counts["fill_descriptions"] + counts["move_dates"] + counts["to_add"]
        return {
            "status": "updated" if changed else "unchanged",
            "reimported": True,
            "statement_id": statement_id,
            "counts": counts,
            "to_check": counts["differs"] + counts["to_review"],
        }

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
        _delete_statement(session, statement, settings.pdf_dir)
        return {"deleted": statement_id}

    @app.post("/api/statements/bulk-delete")
    def bulk_delete_statements(body: dict, user: User = Depends(get_current_user),
                               session: Session = Depends(get_session)) -> dict:
        """Delete several statements. Statements the user does not own are skipped, not failed."""
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise HTTPException(status_code=422, detail="ids must be a list of statement ids")
        deleted, skipped = [], []
        for statement_id in dict.fromkeys(ids):
            statement = session.get(Statement, statement_id)
            if statement is None:
                skipped.append(statement_id)
                continue
            account = account_for_read(session, user, statement.account_id)
            if not access.is_owner(user, account):
                skipped.append(statement_id)
                continue
            _delete_statement(session, statement, settings.pdf_dir)
            deleted.append(statement_id)
        return {"deleted": deleted, "skipped": skipped}

    @app.get("/api/statements/{statement_id}/pdf")
    def statement_pdf(statement_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> Response:
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")
        account_for_read(session, user, statement.account_id)
        data = read_pdf(statement, key, settings.pdf_dir)
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{statement.original_name}"'})

    @app.post("/api/statements/{statement_id}/reimport")
    def reimport_statement(
        statement_id: int,
        apply: bool = False,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> dict:
        """Read a stored statement again. Without apply=true nothing is written; see app/reimport.py."""
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")
        account = account_for_read(session, user, statement.account_id)
        if not access.is_owner(user, account):
            raise HTTPException(status_code=403, detail="only the owner can reimport a statement")
        data = read_pdf(statement, key, settings.pdf_dir)
        try:
            return reimport_from_pdf(
                session, statement, data, saved_passwords(session, user), user_rules(session, user),
                settings.work_dir, apply,
            )
        except ReimportRefused as exc:
            raise _err(422, "reimport_refused", str(exc))
        except PasswordRequired:
            raise _err(422, "password_required", "This PDF is password protected.")
        except NoParserError:
            raise _err(422, "no_parser", "No parser recognises this statement.")

    # ---- transactions -----------------------------------------------------------

    @app.get("/api/transactions")
    def list_transactions(
        account_id: int | None = None, category: str | None = None,
        start: date | None = None, end: date | None = None, q: str | None = None,
        object_name: str | None = None,
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
        if object_name:
            query = query.filter(Transaction.object_name == object_name)
        if transfers == "exclude":
            query = query.filter(Transaction.is_transfer.is_(False))
        elif transfers == "only":
            query = query.filter(Transaction.is_transfer.is_(True))
        total = query.count()
        rows = query.order_by(Transaction.booked_on.desc(), Transaction.id.desc()).offset(offset).limit(min(limit, 1000)).all()
        labels = account_labels(session, user)
        return {"total": total, "items": [serialise(t, labels) for t in rows]}

    @app.get("/api/investments")
    def list_investments(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        """Investment reports the user can see, oldest first per account."""
        ids = visible_ids(session, user)
        labels = account_labels(session, user)
        rows = (session.query(PortfolioSnapshot)
                .join(Statement, Statement.id == PortfolioSnapshot.statement_id)
                .filter(PortfolioSnapshot.account_id.in_(ids), Statement.status == "accepted")
                .order_by(PortfolioSnapshot.account_id, PortfolioSnapshot.period_end).all())
        return [{"account": labels.get(r.account_id, ""), "period_end": r.period_end.isoformat(),
                 "total_assets": str(r.total_assets), "cash": str(r.cash),
                 "term_deposits": json.loads(r.term_deposits or "[]")} for r in rows]

    @app.get("/api/objects")
    def list_objects(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[str]:
        """Names already used as an object, from transactions the user can see and their rules."""
        names = {r[0] for r in accepted_query(session, user).with_entities(Transaction.object_name)
                 .filter(Transaction.object_name.isnot(None)).distinct().all()}
        names |= {r[0] for r in session.query(Rule.object_name)
                  .filter(Rule.owner_id == user.id, Rule.object_name.isnot(None)).distinct().all()}
        return sorted(names, key=str.lower)

    @app.get("/api/transactions/{transaction_id}/similar")
    def similar_transactions(transaction_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        t = session.get(Transaction, transaction_id)
        if t is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        account_for_read(session, user, t.account_id)
        key_text = _description_key(t.description, _ignored_terms(user))
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
        if body.object_name is not None:
            t.object_name = body.object_name.strip() or None
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
                if body.object_name is not None:
                    t.object_name = body.object_name.strip() or None
                updated += 1
        rule_id = None
        if body.save_rule:
            rule_id = _upsert_rule(session, user, body.save_rule, body.category,
                                   (body.object_name or "").strip() or None)
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
                if body.object_name is not None:
                    t.object_name = body.object_name.strip() or None
                updated += 1
        rule_id = (_upsert_rule(session, user, text, body.category, (body.object_name or "").strip() or None)
                   if body.save_rule else None)
        session.commit()
        return {"updated": updated, "rule_id": rule_id}

    @app.get("/api/uncategorised/groups")
    def uncategorised_groups(limit: int = 300, user: User = Depends(get_current_user),
                             session: Session = Depends(get_session)) -> dict:
        """Uncategorised transactions grouped by merchant key, most frequent group first.

        Transfers are left out: they are linked to each other, not categorised.
        """
        rows = (accepted_query(session, user)
                .filter(Transaction.category == UNCATEGORISED, Transaction.is_transfer.is_(False))
                .order_by(Transaction.booked_on.desc(), Transaction.id.desc()).all())
        labels = account_labels(session, user)
        groups: dict[str, dict] = {}
        for t in rows:
            key = _description_key(t.description, _ignored_terms(user)) or t.description.strip().upper()[:30]
            g = groups.setdefault(key, {"key": key, "count": 0, "total": Decimal("0"),
                                        "ids": [], "items": [], "examples": []})
            g["count"] += 1
            g["total"] += t.amount
            g["ids"].append(t.id)
            g["items"].append({"id": t.id, "booked_on": t.booked_on.isoformat(), "account": labels.get(t.account_id, ""),
                               "statement_id": t.statement_id,
                               "description": t.description, "amount": str(t.amount)})
            if len(g["examples"]) < 3 and t.description not in g["examples"]:
                g["examples"].append(t.description)
        ordered = sorted(groups.values(), key=lambda g: (-g["count"], -abs(g["total"]), g["key"]))
        return {
            "total": len(rows),
            "groups": [{"key": g["key"], "count": g["count"], "total": str(g["total"].quantize(Decimal("0.01"))),
                        "examples": g["examples"], "ids": g["ids"], "items": g["items"]}
                       for g in ordered[:min(limit, 1000)]],
        }

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

    @app.get("/api/insights")
    def insights(month: str | None = None, start: str | None = None,
                 user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        """Figures for a period from `start` to `month` (both YYYY-MM, inclusive; start defaults to month).

        The period is compared with the period of the same length just before it. The trend,
        recurring payments and balances stay anchored on `month`. Transfers are left out.
        """
        rows = accepted_query(session, user).filter(Transaction.is_transfer.is_(False)).all()
        with_data = sorted({t.booked_on.strftime("%Y-%m") for t in rows})
        if not with_data:
            return {"month": None}
        if month is None:
            month = with_data[-1]
        if not _valid_month(month):
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        if start is None:
            start = month
        if not _valid_month(start) or start > month:
            raise HTTPException(status_code=422, detail="start must be YYYY-MM and not after month")
        span = (int(month[:4]) * 12 + int(month[5:])) - (int(start[:4]) * 12 + int(start[5:])) + 1
        period = [_shift_month(start, i) for i in range(span)]
        prev_period = [_shift_month(start, i - span) for i in range(span)]
        prev = prev_period[-1]

        by_month: dict[str, list[Transaction]] = defaultdict(list)
        for t in rows:
            by_month[t.booked_on.strftime("%Y-%m")].append(t)

        def totals(items: list[Transaction]) -> tuple[Decimal, Decimal]:
            income = sum((t.amount for t in items if t.amount > 0), Decimal("0"))
            expenses = sum((-t.amount for t in items if t.amount < 0), Decimal("0"))
            return income, expenses

        cur_items = [t for m in period for t in by_month.get(m, [])]
        prev_items = [t for m in prev_period for t in by_month.get(m, [])]
        income, expenses = totals(cur_items)
        prev_income, prev_expenses = totals(prev_items)
        net = income - expenses

        def change(now: Decimal, before: Decimal) -> float | None:
            return round(float((now - before) / before * 100), 1) if before > 0 else None

        earlier = [m for m in with_data if m < start][-3:]
        avg_3m = (sum((totals(by_month[m])[1] for m in earlier), Decimal("0")) / len(earlier)) if earlier else None

        # Spending by category, grouped under the parent, with subcategories.
        parent_now: dict[str, Decimal] = defaultdict(Decimal)
        parent_prev: dict[str, Decimal] = defaultdict(Decimal)
        children: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for t in cur_items:
            if t.amount < 0:
                parent_now[parent_of(t.category)] += -t.amount
                if SEPARATOR in t.category:
                    children[parent_of(t.category)][t.category] += -t.amount
        for t in prev_items:
            if t.amount < 0:
                parent_prev[parent_of(t.category)] += -t.amount
        categories = []
        for name, total in sorted(parent_now.items(), key=lambda kv: kv[1], reverse=True):
            categories.append({
                "category": name, "total": str(total),
                "share": round(float(total / expenses * 100), 1) if expenses else 0.0,
                "change": change(total, parent_prev.get(name, Decimal("0"))),
                "children": [{"category": c, "total": str(v)}
                             for c, v in sorted(children[name].items(), key=lambda kv: kv[1], reverse=True)],
            })

        expense_rows = [t for t in cur_items if t.amount < 0]
        merchants: dict[str, dict] = {}
        for t in expense_rows:
            key = _description_key(t.description, _ignored_terms(user)) or t.description.upper()[:20]
            entry = merchants.setdefault(key, {"name": key, "total": Decimal("0"), "count": 0})
            entry["total"] += -t.amount
            entry["count"] += 1
        top_merchants = sorted(merchants.values(), key=lambda m: m["total"], reverse=True)[:8]

        largest = sorted(expense_rows, key=lambda t: t.amount)[:5]

        # Recurring: the same merchant with spending in at least 3 of the last 6 months.
        window = {_shift_month(month, -i) for i in range(6)}
        recent: dict[str, dict] = {}
        for m in sorted(window):
            for t in by_month.get(m, []):
                if t.amount < 0:
                    key = _description_key(t.description, _ignored_terms(user))
                    if not key:
                        continue
                    entry = recent.setdefault(key, {"name": key, "months": set(), "amounts": []})
                    entry["months"].add(m)
                    entry["amounts"].append(-t.amount)
        recurring = [
            {"name": e["name"], "months": len(e["months"]),
             "average": str((sum(e["amounts"]) / len(e["amounts"])).quantize(Decimal("0.01")))}
            for e in recent.values() if len(e["months"]) >= 3
        ]
        recurring.sort(key=lambda r: Decimal(r["average"]), reverse=True)

        trend = []
        for i in range(11, -1, -1):
            m = _shift_month(month, -i)
            inc, exp = totals(by_month.get(m, []))
            trend.append({"month": m, "income": str(inc), "expenses": str(exp)})

        uncategorised = [t for t in expense_rows if t.category == UNCATEGORISED]

        # Latest accepted statement per account, for balances.
        visible = visible_ids(session, user)
        latest: dict[int, Statement] = {}
        for st in (session.query(Statement).filter(Statement.account_id.in_(visible), Statement.status == "accepted")
                   .order_by(Statement.period_end.desc(), Statement.id.desc()).all()):
            latest.setdefault(st.account_id, st)
        labels = account_labels(session, user)
        balances = []
        for account_id, st in latest.items():
            account = session.get(Account, account_id)
            is_card = account is not None and account.institution in CARD_INSTITUTIONS
            balances.append({"account": labels.get(account_id, ""), "institution": account.institution if account else "",
                             "is_card": is_card, "closing_balance": str(st.closing_balance),
                             "as_of": st.period_end.isoformat()})
        bank_total = sum((Decimal(b["closing_balance"]) for b in balances if not b["is_card"]), Decimal("0"))

        budget_rows = list_budgets(month=month, user=user, session=session)
        alerts = [{"category": b["category"], "status": b["status"], "spent": b["spent"],
                   "monthly_limit": b["monthly_limit"]} for b in budget_rows if b.get("status") in ("over", "near")]

        return {
            "month": month, "start": start, "previous_month": prev,
            "previous_start": prev_period[0], "first_month": with_data[0], "period_months": span,
            "income": str(income), "expenses": str(expenses), "net": str(net),
            "savings_rate": round(float(net / income * 100), 1) if income > 0 else None,
            "previous_income": str(prev_income), "previous_expenses": str(prev_expenses),
            "income_change": change(income, prev_income), "expenses_change": change(expenses, prev_expenses),
            "average_expenses_3m": str(avg_3m.quantize(Decimal("0.01"))) if avg_3m is not None else None,
            "categories": categories,
            "top_merchants": [{"name": m["name"], "total": str(m["total"]), "count": m["count"]} for m in top_merchants],
            "largest_expenses": [{"booked_on": t.booked_on.isoformat(), "description": t.description,
                                  "amount": str(t.amount), "category": t.category} for t in largest],
            "recurring": recurring[:8],
            "trend": trend,
            "uncategorised": {"count": len(uncategorised), "total": str(sum((-t.amount for t in uncategorised), Decimal("0")))},
            "balances": sorted(balances, key=lambda b: (b["is_card"], b["account"])),
            "bank_total": str(bank_total),
            "budget_alerts": alerts,
        }

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

        def renamed(name: str) -> str:
            # Renaming a parent renames its subcategories too: "Old > Child" -> "New > Child".
            return new + name[len(old):] if in_category(name, old) else name

        for other in session.query(Category).all():
            if in_category(other.name, old):
                other.name = renamed(other.name)
        for t in accepted_query(session, user).all():
            if in_category(t.category, old):
                t.category = renamed(t.category)
        for rule in session.query(Rule).filter_by(owner_id=user.id).all():
            if in_category(rule.category, old):
                rule.category = renamed(rule.category)
        for budget in session.query(Budget).filter_by(owner_id=user.id).all():
            if in_category(budget.category, old):
                budget.category = renamed(budget.category)
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

    def _upsert_rule(session: Session, user: User, contains: str, category: str,
                     object_name: str | None = None) -> int:
        ensure_category(session, category)
        existing = session.query(Rule).filter(Rule.owner_id == user.id,
                                              func.upper(Rule.contains) == contains.upper()).first()
        if existing is not None:
            existing.category = category
            if object_name is not None:
                existing.object_name = object_name
            return existing.id
        rule = Rule(owner_id=user.id, contains=contains, category=category, object_name=object_name)
        session.add(rule)
        session.flush()
        return rule.id

    @app.get("/api/rules")
    def list_rules(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        rows = session.query(Rule).filter_by(owner_id=user.id).order_by(Rule.id).all()
        return [{"id": r.id, "contains": r.contains, "category": r.category, "object_name": r.object_name}
                for r in rows]

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
        if not _valid_month(month):
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
            # A budget on a parent also counts its subcategories.
            used = sum((v for k, v in spent.items() if in_category(k, b.category)), Decimal("0"))
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
