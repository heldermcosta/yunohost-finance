"""API and pipeline tests. Statement text is synthetic; PDF extraction is replaced by that text.

Everything else runs for real: hashing, parsing, reconciliation, encryption,
the database, and access rules.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import crypto, ingest
from app.config import Settings
from app.main import create_app
from tests.test_activobank import basic_statement


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


@pytest.fixture
def text_by_pdf(monkeypatch):
    """Maps the fake PDF bytes to the statement text that extraction should return."""
    texts: dict[bytes, str] = {}

    def fake_extract(pdf_bytes: bytes, work_dir):
        return texts[pdf_bytes]

    monkeypatch.setattr(ingest, "_pdf_to_text", fake_extract)
    return texts


@pytest.fixture
def client(settings):
    app = create_app(settings)
    return TestClient(app)


def as_user(name: str) -> dict:
    return {"Auth-User": name}


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
    assert expenses["Groceries"] == "123.45"
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


def test_stored_pdf_is_encrypted_on_disk_and_reads_back(client, settings, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 secret-looking-bytes"
    text_by_pdf[pdf] = basic_statement()
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]

    stored = list(settings.pdf_dir.glob("*.bin"))
    assert len(stored) == 1
    assert b"secret-looking-bytes" not in stored[0].read_bytes()

    back = client.get(f"/api/statements/{statement_id}/pdf", headers=as_user("helder"))
    assert back.status_code == 200
    assert back.content == pdf


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


def test_accepted_statements_cannot_be_deleted_here(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 keep"
    text_by_pdf[pdf] = basic_statement()
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]
    assert client.delete(f"/api/statements/{statement_id}", headers=as_user("helder")).status_code == 409


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
    assert lines[0].startswith("booked_on,account_id,description")
    assert len(lines) == 1 + 3  # header + the three transactions of the accepted statement
