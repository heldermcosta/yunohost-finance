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


def test_same_pdf_twice_is_read_again_and_nothing_is_duplicated(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 once"
    text_by_pdf[pdf] = basic_statement()
    assert upload(client, "helder", account_id, pdf).status_code == 201
    again = upload(client, "helder", account_id, pdf)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "unchanged"
    assert client.get("/api/transactions", headers=as_user("helder")).json()["total"] == 3


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


def test_uncategorised_group_items_name_the_bank_account(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 groups-account"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    groups = client.get("/api/uncategorised/groups", headers=as_user("helder")).json()["groups"]
    assert groups and all(item["account"] for g in groups for item in g["items"])


def test_a_transaction_opens_the_pdf_it_came_from(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 origin-of-transaction"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    items = client.get("/api/transactions", headers=as_user("helder")).json()["items"]
    statement_id = items[0]["statement_id"]
    assert statement_id is not None
    r = client.get(f"/api/statements/{statement_id}/pdf", headers=as_user("helder"))
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_insights_period_is_the_sum_of_its_months(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 insights-period"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    single = client.get("/api/insights?month=2026-05", headers=as_user("helder")).json()
    same = client.get("/api/insights?month=2026-05&start=2026-05", headers=as_user("helder")).json()
    assert same["expenses"] == single["expenses"] and same["income"] == single["income"]
    period = client.get("/api/insights?month=2026-05&start=2026-04", headers=as_user("helder")).json()
    april = client.get("/api/insights?month=2026-04", headers=as_user("helder")).json()
    assert period["period_months"] == 2 and period["start"] == "2026-04"
    assert Decimal(period["expenses"]) == Decimal(april["expenses"]) + Decimal(single["expenses"])
    assert period["previous_start"] == "2026-02"
    assert client.get("/api/insights?month=2026-05&start=2026-06", headers=as_user("helder")).status_code == 422
    assert client.get("/api/insights?month=2026-05&start=bad", headers=as_user("helder")).status_code == 422


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


def test_bulk_delete_removes_owned_statements_and_skips_the_rest(client, text_by_pdf):
    account_id = make_account(client, "helder")
    assert client.post(f"/api/accounts/{account_id}/shares", headers=as_user("helder"),
                       json={"username": "anna"}).status_code == 201
    ids = []
    for name, period in ((b"%PDF-1.4 bulk-a", "2026/05/01 A 2026/05/31"), (b"%PDF-1.4 bulk-b", "2026/06/01 A 2026/06/30")):
        text_by_pdf[name] = basic_statement().replace("2026/05/01 A 2026/05/31", period)
        ids.append(upload(client, "helder", account_id, name).json()["statement_id"])
    # Anna can see the shared account but cannot delete its statements.
    r = client.post("/api/statements/bulk-delete", headers=as_user("anna"), json={"ids": ids})
    assert r.status_code == 200
    assert r.json() == {"deleted": [], "skipped": ids}
    assert len(client.get("/api/statements", headers=as_user("helder")).json()) == 2

    r = client.post("/api/statements/bulk-delete", headers=as_user("helder"), json={"ids": ids + [999999]})
    assert r.json() == {"deleted": ids, "skipped": [999999]}
    assert client.get("/api/statements", headers=as_user("helder")).json() == []
    assert client.get("/api/transactions", headers=as_user("helder")).json()["total"] == 0


def test_bulk_delete_rejects_a_malformed_body(client):
    r = client.post("/api/statements/bulk-delete", headers=as_user("helder"), json={"ids": "1"})
    assert r.status_code == 422


def test_uncategorised_groups_are_sorted_by_frequency(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 groups"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    # Recategorise everything back to Uncategorised so the page has rows to group.
    listed = client.get("/api/transactions?limit=1000", headers=as_user("helder")).json()["items"]
    client.post("/api/transactions/bulk-category", headers=as_user("helder"),
                json={"ids": [t["id"] for t in listed], "category": "Uncategorised"})

    data = client.get("/api/uncategorised/groups", headers=as_user("helder")).json()
    counts = [g["count"] for g in data["groups"]]
    assert counts == sorted(counts, reverse=True)
    assert sum(counts) == data["total"] > 0
    ids = [i for g in data["groups"] for i in g["ids"]]
    assert sorted(ids) == sorted(t["id"] for t in listed if not t["is_transfer"])
    assert all(g["examples"] for g in data["groups"])


def test_ignored_terms_are_removed_before_the_merchant_is_compared():
    from app.main import _description_key

    assert _description_key("PAG SERV DEGIRO INVESTING") == "PAG SERV"
    assert _description_key("PAG SERV DEGIRO INVESTING", ("PAG SERV",)) == "DEGIRO INVESTING"
    # Whole words only: "PAG" inside another word is not removed.
    assert _description_key("PAGAMENTO LIDL", ("PAG",)) == "PAGAMENTO LIDL"
    # Double spaces in the bank text do not stop a term from matching.
    assert _description_key("PAG  SERV  DEGIRO", ("PAG SERV",)) == "DEGIRO"


def test_ignored_terms_are_saved_per_user_and_used_in_the_groups(client, text_by_pdf):
    r = client.put("/api/me/ignored-terms", headers=as_user("helder"),
                   json={"terms": [" pag serv ", "PAG SERV", ""]})
    assert r.status_code == 200
    assert r.json()["ignored_terms"] == ["PAG SERV"]
    assert client.get("/api/me", headers=as_user("helder")).json()["ignored_terms"] == ["PAG SERV"]
    assert client.get("/api/me", headers=as_user("elsa")).json()["ignored_terms"] == []

    too_long = client.put("/api/me/ignored-terms", headers=as_user("helder"), json={"terms": ["X" * 41]})
    assert too_long.status_code == 422


def test_duplicate_upload_is_read_again_for_the_owner(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 same-file"
    text_by_pdf[pdf] = basic_statement()
    assert upload(client, "helder", account_id, pdf, name="first.pdf").status_code == 201
    again = upload(client, "helder", account_id, pdf, name="copy.pdf")
    assert again.status_code == 200
    assert again.json()["status"] == "unchanged"

    # Elsa cannot see Helder's account at all, so she gets no hint of its statements.
    assert upload(client, "elsa", account_id, pdf, name="copy.pdf").status_code == 404


def test_object_is_set_in_bulk_filtered_and_listed(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 objects"
    text_by_pdf[pdf] = basic_statement()
    upload(client, "helder", account_id, pdf)
    items = client.get("/api/transactions?limit=1000", headers=as_user("helder")).json()["items"]
    ids = [t["id"] for t in items][:2]

    r = client.post("/api/transactions/bulk-category", headers=as_user("helder"),
                    json={"ids": ids, "category": "Housing > Water", "object_name": "  House A "})
    assert r.status_code == 200 and r.json()["updated"] == 2

    listed = client.get("/api/transactions?object_name=House%20A", headers=as_user("helder")).json()
    assert listed["total"] == 2
    assert all(t["object_name"] == "House A" for t in listed["items"])
    assert client.get("/api/objects", headers=as_user("helder")).json() == ["House A"]
    assert client.get("/api/objects", headers=as_user("elsa")).json() == []

    # An empty object clears it; leaving the field out keeps it.
    client.post("/api/transactions/bulk-category", headers=as_user("helder"),
                json={"ids": ids[:1], "category": "Housing > Water"})
    assert client.get("/api/transactions?object_name=House%20A", headers=as_user("helder")).json()["total"] == 2
    client.post("/api/transactions/bulk-category", headers=as_user("helder"),
                json={"ids": ids[:1], "category": "Housing > Water", "object_name": ""})
    assert client.get("/api/transactions?object_name=House%20A", headers=as_user("helder")).json()["total"] == 1


def test_rule_object_is_taken_from_the_first_matching_rule():
    from app.categories import rule_object

    rules = [("DEGIRO", "Investing", "Portfolio"), ("LIDL", "Groceries", None)]
    assert rule_object("PAG SERV DEGIRO", rules) == "Portfolio"
    assert rule_object("LIDL SETUBAL", rules) is None
    assert rule_object("CONTINENTE", rules) is None


# --- Banco Invest reports ---------------------------------------------------

from decimal import Decimal as _D  # noqa: E402


from app.parsers import find_parser
from app.parsers.base import reconcile

REPORT = "\n".join([
    "Exmo. Sr.",
    "Banco Invest, SA - Capital Social €47.500.000 - Av. Eng. Duarte Pacheco, Lisboa",
    "Posição Integrada - 31 de Janeiro de 2016",
    "Cliente:      754952",
    "Depósitos à Ordem                                  Saldo Anterior:     50,81",
    "  Conta 754952-10-1            EUR",
    "Data                  Data                  Descrição                  Débito      Crédito     Saldo",
    "2016-01-05            2016-01-05            Subscrição Fundo: ARPPR    50,00       0,00        0,81",
    "2016-01-26            2016-01-26            Transferência de 0754952-10-002   0,00   50,00   50,81",
    "  Saldo em      2016-01-31      :      50,81",
    "Aplicações a Prazo",
    "Invest + 180 dias    2015-12-07    2016-06-04    1,50    4.035,00    EUR",
    "Total de Activos:                58.244,73",
])


def test_invest_movements_reconcile_and_snapshot_is_kept():
    parser = find_parser(REPORT)
    assert parser.institution == "invest"
    st = parser.parse(REPORT)
    assert st.account_ref == "754952"
    assert st.period_end.isoformat() == "2016-01-31"
    assert [t.amount for t in st.transactions] == [_D("-50.00"), _D("50.00")]
    assert reconcile(st).status == "ok"
    assert st.snapshot["total_assets"] == "58244.73"
    assert st.snapshot["cash"] == "50.81"
    assert st.snapshot["term_deposits"] == [{
        "description": "Invest + 180 dias", "start": "2015-12-07", "end": "2016-06-04",
        "rate": "1,50", "amount": "4035.00", "currency": "EUR",
    }]


def test_investment_report_is_listed_and_its_snapshot_removed_with_it(client, text_by_pdf):
    created = client.post("/api/accounts", headers={"Authorization": "Basic aGVsZGVyOi0="},
                          json={"institution": "invest", "label": "Invest Helder", "account_ref_last4": "4952"})
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]
    pdf = b"%PDF-1.4 invest"
    text_by_pdf[pdf] = REPORT
    up = client.post("/api/statements", headers={"Authorization": "Basic aGVsZGVyOi0="},
                     data={"account_id": str(account_id)},
                     files={"file": ("invest.pdf", pdf, "application/pdf")})
    assert up.status_code == 201, up.text
    statement_id = up.json()["statement_id"]

    listed = client.get("/api/investments", headers={"Authorization": "Basic aGVsZGVyOi0="}).json()
    assert listed == [{"account": "Invest Helder", "period_end": "2016-01-31", "total_assets": "58244.73",
                       "cash": "50.81",
                       "term_deposits": [{"description": "Invest + 180 dias", "start": "2015-12-07",
                                          "end": "2016-06-04", "rate": "1,50", "amount": "4035.00",
                                          "currency": "EUR"}]}]

    assert client.delete(f"/api/statements/{statement_id}",
                         headers={"Authorization": "Basic aGVsZGVyOi0="}).status_code == 200
    assert client.get("/api/investments", headers={"Authorization": "Basic aGVsZGVyOi0="}).json() == []


# --- Banco Carregosa monthly summaries ----------------------------------------

CARREGOSA_2019 = "\n".join([
    "Banco L.J. Carregosa, S.A.",
    "Conta n.º: 6158838",
    "Resumo Integrado de 2019-09-01 a 2019-09-30",
    "Património da conta nº 6158838 a 2019-09-30 (EUR)",
    "Total Ativos 43.388,11",
    "Extrato de Movimentos por Moeda",
    "Moeda: EUR / GoBulling Pro",
    "Saldo Inicial 71,46",
    "Saldo Final 3,85",
    "Moeda: EUR / Depósitos à Ordem Transporte",
    "2019-09-16 2019-09-16 Transferência de GoBulling Pro 110,00 110,00",
    "2019-09-16 2019-09-16 Transferência para PT50002300004550322098894 -110,00 0,00",
    "Saldo Final 0,00",
    "Saldo Disponível 0,00",
])

CARREGOSA_2025 = "\n".join([
    "Banco L. J. Carregosa, S.A. | Capital Social € 20.000.000,00",
    "Período: 2025-10-01 a 2025-10-31",
    "Conta n.º: 6158838",
    "Serve o presente para remeter a V/ Exa(s). o seguinte Resumo Integrado.",
    "Total Ativos 3 030,79 Total Passivos 0,00",
    "Movimentos Depósitos à Ordem",
    "Data Mov. Data Valor Descrição Valor Saldo",
    "Depósitos à Ordem - EUR",
    "Saldo Contabilístico Inicial 0,00",
    "Não há movimentos para o período.",
    "Saldo Contabilístico Final 0,00",
    "GoBulling - EUR",
    "Saldo Contabilístico Inicial 13,06",
    "Saldo Contabilístico Final 13,06",
])


def test_carregosa_monthly_summary_reconciles_the_deposit_account_and_keeps_totals():
    from app.parsers import find_parser
    from app.parsers.base import reconcile

    st = find_parser(CARREGOSA_2019).parse(CARREGOSA_2019)
    assert st.institution == "carregosa" and st.account_ref == "6158838"
    assert [t.amount for t in st.transactions] == [Decimal("110.00"), Decimal("-110.00")]
    assert reconcile(st).status == "ok"
    assert st.snapshot["total_assets"] == "43388.11"

    st = find_parser(CARREGOSA_2025).parse(CARREGOSA_2025)
    assert st.period_end.isoformat() == "2025-10-31"
    assert st.transactions == [] and reconcile(st).status == "ok"
    assert st.snapshot["total_assets"] == "3030.79"


def test_carregosa_daily_ledger_reports_are_not_imported():
    import pytest
    from app.parsers import NoParserError, find_parser

    daily = "Relatório de Lançamentos\nCarregosa\nConta n.º: 6158838\nDepósitos à Ordem\nSaldo Final 0,00"
    with pytest.raises(NoParserError):
        find_parser(daily)
