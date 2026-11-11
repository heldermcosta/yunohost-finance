"""API and pipeline tests. Statement text is synthetic; PDF extraction is replaced by that text.

Everything else runs for real: hashing, parsing, reconciliation, encryption,
the database, and access rules.
"""

from __future__ import annotations

import base64
import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import crypto, ingest
from app.categories import SEPARATOR
from app.config import Settings
from app.parsers.pdftext import PdfPasswordError
from app.main import create_app
from tests.test_activobank import basic_statement
from app.categories import SEPARATOR


@pytest.fixture
def settings(tmp_path):
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        database_url=f"sqlite:///{data / 'finance.db'}",
        key_file=tmp_path / "keys" / "secret.key",
        work_dir=data / "work",
        dev_user=None,
    )


class FakePdfs(dict):
    """Maps fake PDF bytes to the statement text. `locks` maps protected PDFs to their password."""

    def __init__(self):
        super().__init__()
        self.locks: dict[bytes, str] = {}


@pytest.fixture
def text_by_pdf(monkeypatch):
    texts = FakePdfs()

    def fake_extract(pdf_bytes: bytes, work_dir, password=None):
        if pdf_bytes in texts.locks and password != texts.locks[pdf_bytes]:
            raise PdfPasswordError("PDF is password protected")
        return texts[pdf_bytes]

    monkeypatch.setattr(ingest, "_pdf_to_text", fake_extract)
    return texts


@pytest.fixture
def client(settings):
    app = create_app(settings)
    return TestClient(app)


def as_user(name: str) -> dict:
    # What SSOwat sends: Basic auth with the username and the password "-".
    token = base64.b64encode(f"{name}:-".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def upload(client, user, account_id, pdf: bytes, name="EXTRATO COMBINADO 2026005.pdf"):
    return client.post(
        "/api/statements",
        headers=as_user(user),
        data={"account_id": str(account_id)},
        files={"file": (name, pdf, "application/pdf")},
    )


def make_account(client, user, label="ActivoBank Helder"):
    r = client.post("/api/accounts", headers=as_user(user),
                    json={"institution": "activobank", "label": label, "account_ref_last4": "3456"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- crypto ---------------------------------------------------------------

def test_encryption_round_trip_and_binding(tmp_path):
    key = crypto.load_or_create_key(tmp_path / "k")
    blob = crypto.encrypt(key, b"%PDF-1.4 data", associated=b"abc")
    assert crypto.decrypt(key, blob, associated=b"abc") == b"%PDF-1.4 data"
    with pytest.raises(Exception):
        crypto.decrypt(key, blob, associated=b"other")


def test_key_file_is_private_and_never_overwritten(tmp_path):
    path = tmp_path / "k"
    first = crypto.load_or_create_key(path)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert crypto.load_or_create_key(path) == first


# --- import and review -----------------------------------------------------

def test_clean_statement_is_accepted_and_counts_in_summary(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 clean"
    text_by_pdf[pdf] = basic_statement()

    r = upload(client, "helder", account_id, pdf)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "accepted"
    assert r.json()["reconciliation"] == "ok"

    summary = client.get("/api/summary/monthly", headers=as_user("helder")).json()
    expenses = {row["category"]: row["expenses"] for row in summary}
    assert expenses["Groceries > Supermarket"] == "123.45"
    assert expenses["Uncategorised"] == "50.00"  # "PAG SERV EXEMPLO" matches no rule


def test_failed_statement_goes_to_review_and_stays_out_of_analytics(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 bad total"
    text_by_pdf[pdf] = basic_statement(closing_override="2 026.56")

    r = upload(client, "helder", account_id, pdf)
    assert r.json()["status"] == "review"
    assert r.json()["reconciliation"] == "failed"

    queue = client.get("/api/review", headers=as_user("helder")).json()
    assert [q["id"] for q in queue] == [r.json()["statement_id"]]
    assert client.get("/api/summary/monthly", headers=as_user("helder")).json() == []

    accepted = client.post(f"/api/statements/{r.json()['statement_id']}/accept", headers=as_user("helder"))
    assert accepted.status_code == 200
    assert client.get("/api/review", headers=as_user("helder")).json() == []
    assert len(client.get("/api/summary/monthly", headers=as_user("helder")).json()) > 0


def test_same_pdf_twice_is_refused(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 once"
    text_by_pdf[pdf] = basic_statement()
    assert upload(client, "helder", account_id, pdf).status_code == 201
    assert upload(client, "helder", account_id, pdf).status_code == 409


def test_wrong_institution_is_refused(client, text_by_pdf):
    account_id = client.post("/api/accounts", headers=as_user("helder"),
                             json={"institution": "bankinter", "label": "Bankinter"}).json()["id"]
    pdf = b"%PDF-1.4 wrong bank"
    text_by_pdf[pdf] = basic_statement()
    assert upload(client, "helder", account_id, pdf).status_code == 422


def test_non_pdf_upload_is_refused(client, text_by_pdf):
    account_id = make_account(client, "helder")
    r = upload(client, "helder", account_id, b"plain text, not a pdf")
    assert r.status_code == 422


def test_stored_pdf_is_kept_as_uploaded_and_reads_back(client, settings, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 plain-bytes"
    text_by_pdf[pdf] = basic_statement()
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]

    stored = list(settings.pdf_dir.glob("*.pdf"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == pdf

    back = client.get(f"/api/statements/{statement_id}/pdf", headers=as_user("helder"))
    assert back.status_code == 200
    assert back.content == pdf


def test_protected_pdf_asks_for_password_then_remembers_it(client, text_by_pdf):
    account_id = make_account(client, "helder")
    locked = b"%PDF-1.4 locked-one"
    text_by_pdf[locked] = basic_statement()
    text_by_pdf.locks[locked] = "secret-1"

    r = upload(client, "helder", account_id, locked)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "password_required"

    wrong = client.post("/api/statements", headers=as_user("helder"),
                        data={"account_id": str(account_id), "pdf_password": "nope"},
                        files={"file": ("EXTRATO 1.pdf", locked, "application/pdf")})
    assert wrong.status_code == 422

    ok = client.post("/api/statements", headers=as_user("helder"),
                     data={"account_id": str(account_id), "pdf_password": "secret-1"},
                     files={"file": ("EXTRATO 1.pdf", locked, "application/pdf")})
    assert ok.status_code == 201, ok.text
    assert "pdf_password" not in ok.json()

    saved = client.get("/api/pdf-passwords", headers=as_user("helder")).json()
    assert len(saved) == 1
    assert "secret-1" not in str(saved)  # labels only, never the password

    other = b"%PDF-1.4 locked-two"
    text_by_pdf[other] = basic_statement(closing_override="2 026.56")
    text_by_pdf.locks[other] = "secret-1"
    again = upload(client, "helder", account_id, other, name="EXTRATO 2.pdf")
    assert again.status_code == 201, again.text
    assert again.json()["status"] == "review"


def test_saved_password_can_be_deleted_and_is_private_to_its_owner(client):
    created = client.post("/api/pdf-passwords", headers=as_user("helder"),
                          json={"label": "Cards", "password": "abc"}).json()
    assert client.delete(f"/api/pdf-passwords/{created['id']}", headers=as_user("elsa")).status_code == 404
    assert client.get("/api/pdf-passwords", headers=as_user("elsa")).json() == []
    assert client.delete(f"/api/pdf-passwords/{created['id']}", headers=as_user("helder")).status_code == 200
    assert client.get("/api/pdf-passwords", headers=as_user("helder")).json() == []


# --- access ----------------------------------------------------------------

def test_other_user_cannot_see_an_unshared_account(client, text_by_pdf):
    account_id = make_account(client, "helder")
    assert client.get("/api/accounts", headers=as_user("elsa")).json() == []
    pdf = b"%PDF-1.4 private"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    assert upload(client, "elsa", account_id, b"%PDF-1.4 intrusion").status_code == 404


def test_shared_account_is_visible_but_not_acceptable_by_the_other_user(client, text_by_pdf):
    account_id = make_account(client, "helder", label="Joint")
    assert client.post(f"/api/accounts/{account_id}/shares", headers=as_user("helder"),
                       json={"username": "elsa"}).status_code == 201

    assert [a["id"] for a in client.get("/api/accounts", headers=as_user("elsa")).json()] == [account_id]

    pdf = b"%PDF-1.4 joint bad"
    text_by_pdf[pdf] = basic_statement(closing_override="2 026.56")
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]
    r = client.post(f"/api/statements/{statement_id}/accept", headers=as_user("elsa"))
    assert r.status_code == 403


def test_only_owner_can_share(client):
    account_id = make_account(client, "helder")
    r = client.post(f"/api/accounts/{account_id}/shares", headers=as_user("elsa"), json={"username": "elsa"})
    assert r.status_code == 404


def test_accepted_statement_is_deleted_with_its_transactions_and_can_be_reimported(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 delete-me"
    text_by_pdf[pdf] = basic_statement()
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]
    before = client.get("/api/transactions", headers=as_user("helder")).json()["total"]
    assert before > 0
    r = client.delete(f"/api/statements/{statement_id}", headers=as_user("helder"))
    assert r.status_code == 200, r.text
    assert client.get("/api/transactions", headers=as_user("helder")).json()["total"] == 0
    assert client.get("/api/statements", headers=as_user("helder")).json() == []
    # The same file can be imported again after its deletion.
    assert upload(client, "helder", account_id, pdf).status_code == 201


def test_only_the_owner_can_delete_a_statement(client, text_by_pdf):
    account_id = make_account(client, "helder")
    assert client.post(f"/api/accounts/{account_id}/shares", headers=as_user("helder"),
                       json={"username": "anna"}).status_code == 201
    pdf = b"%PDF-1.4 owner-only"
    text_by_pdf[pdf] = basic_statement()
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]
    assert client.delete(f"/api/statements/{statement_id}", headers=as_user("anna")).status_code == 403
    listed = client.get("/api/statements", headers=as_user("anna")).json()
    assert listed and listed[0]["can_delete"] is False


def test_statement_list_shows_transaction_counts_and_owner_flag(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 listed"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    rows = client.get("/api/statements", headers=as_user("helder")).json()
    assert len(rows) == 1
    assert rows[0]["transactions"] > 0 and rows[0]["can_delete"] is True
    assert rows[0]["status"] == "accepted"


def test_account_bank_can_be_changed(client):
    account_id = make_account(client, "helder")
    r = client.patch(f"/api/accounts/{account_id}", headers=as_user("helder"), json={"institution": " Bankinter "})
    assert r.status_code == 200, r.text
    accounts = client.get("/api/accounts", headers=as_user("helder")).json()
    assert [a["institution"] for a in accounts if a["id"] == account_id] == ["bankinter"]


def test_unsigned_request_is_rejected(client):
    assert client.get("/api/me").status_code == 401


def test_scope_can_be_set_and_is_validated(client):
    make_account(client, "helder")
    assert client.put("/api/me/scope", headers=as_user("helder"), json={"view_scope": "all"}).status_code == 200
    assert client.put("/api/me/scope", headers=as_user("helder"), json={"view_scope": "nonsense"}).status_code == 422


def test_csv_export_contains_only_accepted_rows(client, text_by_pdf):
    account_id = make_account(client, "helder")
    ok_pdf, bad_pdf = b"%PDF-1.4 ok", b"%PDF-1.4 bad"
    text_by_pdf[ok_pdf] = basic_statement()
    text_by_pdf[bad_pdf] = basic_statement(closing_override="2 026.56")
    upload(client, "helder", account_id, ok_pdf)
    upload(client, "helder", account_id, bad_pdf, name="EXTRATO COMBINADO 2026006.pdf")

    r = client.get("/api/export/transactions.csv", headers=as_user("helder"))
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0].startswith("booked_on,account,description")
    assert len(lines) == 1 + 3  # header + the three transactions of the accepted statement


def test_web_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "api/" in r.text


# --- account matching, rules, budgets, exports -----------------------------

def _account(client, user, last4, label):
    r = client.post("/api/accounts", headers=as_user(user),
                    json={"institution": "ActivoBank", "label": label, "account_ref_last4": last4})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_statement_goes_to_the_account_with_matching_number(client, text_by_pdf):
    other = _account(client, "helder", "9999", "Other ActivoBank")
    matching = _account(client, "helder", "3456", "Main ActivoBank")  # statement is account 123456
    pdf = b"%PDF-1.4 auto-match"
    text_by_pdf[pdf] = basic_statement()
    r = client.post("/api/statements", headers=as_user("helder"),
                    files={"file": ("EXTRATO.pdf", pdf, "application/pdf")})
    assert r.status_code == 201, r.text
    rows = client.get("/api/transactions", headers=as_user("helder")).json()["items"]
    assert rows and all(t["account_id"] == matching for t in rows)
    assert other not in {t["account_id"] for t in rows}


def test_unnumbered_account_is_not_assigned_automatically(client, text_by_pdf):
    # An account without a number must not absorb a statement of another account.
    r = client.post("/api/accounts", headers=as_user("helder"),
                    json={"institution": "ActivoBank", "label": "Unnumbered"})
    assert r.status_code == 201, r.text
    unnumbered = r.json()["id"]
    pdf = b"%PDF-1.4 no-auto-assign"
    text_by_pdf[pdf] = basic_statement()
    r = client.post("/api/statements", headers=as_user("helder"),
                    files={"file": ("EXTRATO.pdf", pdf, "application/pdf")})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "account_needed"
    assert unnumbered in [a["id"] for a in detail["accounts"]]
    rows = client.get("/api/transactions", headers=as_user("helder")).json()["items"]
    assert rows == []
    accounts = client.get("/api/accounts", headers=as_user("helder")).json()
    assert all(a["account_ref_last4"] in (None, "") for a in accounts if a["id"] == unnumbered)


def test_statement_for_an_unknown_number_asks_for_an_account(client, text_by_pdf):
    _account(client, "helder", "9999", "Only other")
    pdf = b"%PDF-1.4 unknown-number"
    text_by_pdf[pdf] = basic_statement()
    r = client.post("/api/statements", headers=as_user("helder"),
                    files={"file": ("EXTRATO.pdf", pdf, "application/pdf")})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "account_needed"


def test_explicit_account_with_a_different_number_is_refused(client, text_by_pdf):
    wrong = _account(client, "helder", "9999", "Wrong one")
    pdf = b"%PDF-1.4 wrong-number"
    text_by_pdf[pdf] = basic_statement()
    r = upload(client, "helder", wrong, pdf)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "wrong_account"


def test_rule_recategorises_matching_transactions_but_not_manual_ones(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 rules"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    rows = client.get("/api/transactions", headers=as_user("helder")).json()["items"]
    target = rows[0]
    client.patch(f"/api/transactions/{target['id']}", headers=as_user("helder"), json={"category": "Manual pick"})

    r = client.post("/api/rules", headers=as_user("helder"),
                    json={"contains": "PAG SERV", "category": "Services", "apply": True})
    assert r.status_code == 201, r.text
    assert r.json()["updated"] >= 1
    after = {t["id"]: t for t in client.get("/api/transactions", headers=as_user("helder")).json()["items"]}
    assert after[target["id"]]["category"] == "Manual pick"  # manual choices are kept
    assert any(t["category"] == "Services" and t["category_source"] == "rule" for t in after.values())


def test_budget_shows_spend_and_status(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 budget"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    assert client.put("/api/budgets", headers=as_user("helder"),
                      json={"category": "Groceries", "monthly_limit": "100.00"}).status_code == 200
    budgets = client.get("/api/budgets?month=2026-05", headers=as_user("helder")).json()
    groceries = next(b for b in budgets if b["category"] == "Groceries")
    assert groceries["spent"] == "123.45"
    assert groceries["status"] == "over"


def test_exports_have_the_right_formats(client, text_by_pdf):
    import json as jsonlib
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 exports"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    xlsx = client.get("/api/export/transactions.xlsx", headers=as_user("helder"))
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    pdf_out = client.get("/api/export/report.pdf?year=2026", headers=as_user("helder"))
    assert pdf_out.status_code == 200 and pdf_out.content.startswith(b"%PDF")
    backup = jsonlib.loads(client.get("/api/export/backup.json", headers=as_user("helder")).content)
    assert backup["format"] == "yunohost-finance-backup"
    assert len(backup["transactions"]) == 3
    assert "password" not in str(backup).lower()


def test_account_with_statements_cannot_be_deleted(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 no-delete"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    assert client.delete(f"/api/accounts/{account_id}", headers=as_user("helder")).status_code == 409
    empty = _account(client, "helder", "", "Empty")
    assert client.delete(f"/api/accounts/{empty}", headers=as_user("helder")).status_code == 200


def test_upload_without_a_matching_account_suggests_one_to_create(client, text_by_pdf):
    pdf = b"%PDF-1.4 suggest"
    text_by_pdf[pdf] = basic_statement()
    r = client.post("/api/statements", headers=as_user("helder"),
                    files={"file": ("EXTRATO.pdf", pdf, "application/pdf")})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "account_needed"
    assert detail["suggested"] == {"institution": "activobank", "account_ref_last4": "3456",
                                   "label": "ActivoBank ···3456"}


def test_bulk_category_by_text_updates_matching_rows_and_can_save_a_rule(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 bulk-text"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    r = client.post("/api/transactions/bulk-category-by-text", headers=as_user("helder"),
                    json={"text": "PAG SERV", "category": "Services test", "save_rule": True})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] >= 1 and r.json()["rule_id"] is not None
    items = client.get("/api/transactions?q=PAG SERV", headers=as_user("helder")).json()["items"]
    assert items and all(t["category"] == "Services test" for t in items)


def test_budget_on_parent_counts_its_subcategories(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 sub-budget"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    r = client.put("/api/budgets", headers=as_user("helder"),
                   json={"category": "Groceries", "monthly_limit": "500"})
    assert r.status_code in (200, 201), r.text
    statuses = client.get("/api/budgets?month=2026-05", headers=as_user("helder")).json()
    budget = next(b for b in statuses if b["category"] == "Groceries")
    assert Decimal(budget["spent"]) == Decimal("123.45")


def test_renaming_a_parent_renames_its_subcategories(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 rename-parent"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    cats = client.get("/api/categories", headers=as_user("helder")).json()
    groceries = next(c for c in cats if c["name"] == "Groceries")
    assert groceries["id"] is not None
    r = client.patch(f"/api/categories/{groceries['id']}", headers=as_user("helder"), json={"name": "Food"})
    assert r.status_code == 200, r.text
    names = [c["name"] for c in client.get("/api/categories", headers=as_user("helder")).json()]
    assert f"Food{SEPARATOR}Supermarket" in names
    assert f"Groceries{SEPARATOR}Supermarket" not in names
    rows = client.get("/api/transactions", headers=as_user("helder")).json()["items"]
    assert any(t["category"] == f"Food{SEPARATOR}Supermarket" for t in rows)


def test_insights_for_a_month(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 insights"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    r = client.get("/api/insights?month=2026-05", headers=as_user("helder"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["month"] == "2026-05" and data["previous_month"] == "2026-04"
    assert Decimal(data["expenses"]) > 0
    assert Decimal(data["net"]) == Decimal(data["income"]) - Decimal(data["expenses"])
    assert len(data["trend"]) == 12 and data["trend"][-1]["month"] == "2026-05"
    assert data["categories"] and data["categories"][0]["share"] > 0
    assert data["largest_expenses"][0]["amount"].startswith("-")
    assert data["balances"] and data["balances"][0]["closing_balance"]
    assert client.get("/api/insights?month=2026-13", headers=as_user("helder")).status_code == 422


def test_insights_empty_database(client):
    make_account(client, "helder")
    assert client.get("/api/insights", headers=as_user("helder")).json() == {"month": None}
