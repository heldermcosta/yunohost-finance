"""HTTP API. Every endpoint needs a user; on YunoHost that comes from SSO, in development from FINANCE_DEV_USER."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from . import access, crypto
from .auth import username_from_authorization
from .config import Settings
from .ingest import DuplicateStatement, WrongInstitution, import_pdf, read_pdf
from .models import Account, AccountShare, Base, Statement, Transaction, User
from .parsers import NoParserError

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
STATIC_DIR = Path(__file__).resolve().parent / "static"


class AccountIn(BaseModel):
    institution: str = Field(min_length=1, max_length=50)
    label: str = Field(min_length=1, max_length=120)
    account_ref_last4: str = Field(default="", max_length=4)
    currency: str = Field(default="EUR", min_length=3, max_length=3)


class ShareIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)


class ScopeIn(BaseModel):
    view_scope: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    key = crypto.load_or_create_key(settings.key_file)

    app = FastAPI(title="yunohost-finance", version="0.1.0")
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

    def account_or_404(session: Session, user: User, account_id: int) -> Account:
        account = session.get(Account, account_id)
        if account is None or not access.can_access(session, user, account):
            raise HTTPException(status_code=404, detail="account not found")
        return account

    def scoped_account_ids(session: Session, user: User) -> list[int]:
        accounts = access.accessible_accounts(session, user)
        # "own_shared" and "all" select the same accounts today. See access.py.
        if user.view_scope not in access.VIEW_SCOPES:
            raise HTTPException(status_code=500, detail="invalid view scope on user")
        return [a.id for a in accounts]

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

    @app.get("/api/accounts")
    def list_accounts(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        return [
            {
                "id": a.id,
                "institution": a.institution,
                "label": a.label,
                "account_ref_last4": a.account_ref_last4,
                "currency": a.currency,
                "owner": session.get(User, a.owner_id).username,
                "is_owner": a.owner_id == user.id,
            }
            for a in access.accessible_accounts(session, user)
        ]

    @app.post("/api/accounts", status_code=201)
    def create_account(body: AccountIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        account = Account(owner_id=user.id, institution=body.institution, label=body.label,
                          account_ref_last4=body.account_ref_last4, currency=body.currency.upper())
        session.add(account)
        session.commit()
        return {"id": account.id}

    @app.post("/api/accounts/{account_id}/shares", status_code=201)
    def share_account(account_id: int, body: ShareIn, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        account = session.get(Account, account_id)
        if account is None or account.owner_id != user.id:
            raise HTTPException(status_code=404, detail="account not found")
        other = access.get_or_create_user(session, body.username)
        if session.get(AccountShare, (account.id, other.id)) is None:
            session.add(AccountShare(account_id=account.id, user_id=other.id))
            session.commit()
        return {"shared_with": other.username}

    @app.post("/api/statements", status_code=201)
    async def upload_statement(
        account_id: int = Form(...),
        file: UploadFile = File(...),
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> dict:
        account = session.get(Account, account_id)
        if account is None or account.owner_id != user.id:
            raise HTTPException(status_code=404, detail="account not found")

        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="file too large")
        if not data.startswith(b"%PDF"):
            raise HTTPException(status_code=422, detail="not a PDF file")

        try:
            result = import_pdf(
                session,
                pdf_bytes=data,
                original_name=file.filename or "statement.pdf",
                account=account,
                uploaded_by_id=user.id,
                key=key,
                pdf_dir=settings.pdf_dir,
                work_dir=settings.work_dir,
            )
        except DuplicateStatement as exc:
            raise HTTPException(status_code=409, detail=f"already imported as statement {exc.statement_id}")
        except WrongInstitution as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except NoParserError:
            raise HTTPException(status_code=422, detail="no parser recognises this statement yet")
        return asdict(result)

    @app.get("/api/review")
    def review_queue(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
        ids = scoped_account_ids(session, user)
        rows = session.query(Statement).filter(Statement.account_id.in_(ids), Statement.status == "review").order_by(Statement.period_start).all()
        return [
            {
                "id": s.id,
                "account_id": s.account_id,
                "original_name": s.original_name,
                "period": [s.period_start.isoformat(), s.period_end.isoformat()],
                "reconciliation": s.reconciliation,
                "notes": s.notes.split("\n") if s.notes else [],
            }
            for s in rows
        ]

    @app.post("/api/statements/{statement_id}/accept")
    def accept_statement(statement_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")
        account = account_or_404(session, user, statement.account_id)
        if account.owner_id != user.id:
            raise HTTPException(status_code=403, detail="only the owner can accept a statement")
        statement.status = "accepted"
        session.commit()
        return {"id": statement.id, "status": statement.status}

    @app.delete("/api/statements/{statement_id}")
    def reject_statement(statement_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")
        account = account_or_404(session, user, statement.account_id)
        if account.owner_id != user.id:
            raise HTTPException(status_code=403, detail="only the owner can delete a statement")
        if statement.status == "accepted":
            raise HTTPException(status_code=409, detail="accepted statements are not deleted here")
        pdf_path = settings.pdf_dir / statement.pdf_file
        session.delete(statement)
        session.commit()
        pdf_path.unlink(missing_ok=True)
        return {"deleted": statement_id}

    @app.get("/api/statements/{statement_id}/pdf")
    def statement_pdf(statement_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> Response:
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")
        account_or_404(session, user, statement.account_id)
        data = read_pdf(statement, key, settings.pdf_dir)
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{statement.original_name}"'})

    def accepted_transactions(session: Session, user: User, account_id: int | None = None,
                              start: date | None = None, end: date | None = None) -> list[Transaction]:
        ids = scoped_account_ids(session, user)
        if account_id is not None:
            if account_id not in ids:
                raise HTTPException(status_code=404, detail="account not found")
            ids = [account_id]
        query = (session.query(Transaction).join(Statement, Statement.id == Transaction.statement_id)
                 .filter(Transaction.account_id.in_(ids), Statement.status == "accepted"))
        if start:
            query = query.filter(Transaction.booked_on >= start)
        if end:
            query = query.filter(Transaction.booked_on <= end)
        return query.order_by(Transaction.booked_on, Transaction.id).all()

    @app.get("/api/transactions")
    def list_transactions(account_id: int | None = None, start: date | None = None, end: date | None = None,
                          category: str | None = None, user: User = Depends(get_current_user),
                          session: Session = Depends(get_session)) -> list[dict]:
        rows = accepted_transactions(session, user, account_id, start, end)
        if category:
            rows = [t for t in rows if t.category == category]
        return [
            {"id": t.id, "account_id": t.account_id, "booked_on": t.booked_on.isoformat(),
             "description": t.description, "amount": str(t.amount), "category": t.category}
            for t in rows
        ]

    @app.get("/api/summary/monthly")
    def monthly_summary(year: int | None = None, user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)) -> list[dict]:
        start = date(year, 1, 1) if year else None
        end = date(year, 12, 31) if year else None
        totals: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: {"income": Decimal("0"), "expenses": Decimal("0")})
        for t in accepted_transactions(session, user, None, start, end):
            key = (t.booked_on.strftime("%Y-%m"), t.category)
            bucket = "income" if t.amount > 0 else "expenses"
            totals[key][bucket] += abs(t.amount)
        return [
            {"month": month, "category": category, "income": str(v["income"]), "expenses": str(v["expenses"])}
            for (month, category), v in sorted(totals.items())
        ]

    @app.get("/api/export/transactions.csv")
    def export_csv(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> StreamingResponse:
        rows = accepted_transactions(session, user)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["booked_on", "account_id", "description", "amount", "balance", "category"])
        for t in rows:
            writer.writerow([t.booked_on.isoformat(), t.account_id, t.description, str(t.amount), str(t.balance), t.category])
        buffer.seek(0)
        return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="transactions.csv"'})

    return app
