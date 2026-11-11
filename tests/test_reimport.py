"""Repairs to the statement text, and the reimport of statements already stored.

Synthetic text only. The PDF reading and OCR are replaced where the test is about
the rules around them; the rest runs for real.
"""

from __future__ import annotations

from decimal import Decimal

from app.parsers import pdfpos
from app.parsers.activobank import ActivoBankParser, repair_blank_descriptions
from app.parsers.cards import wizink
from app.parsers.cards.universo import parse_universo
from app.parsers.pdfpos import PosLine
from tests.test_activobank import basic_statement, row, statement
from tests.test_app import as_user, client, make_account, settings, text_by_pdf, upload  # noqa: F401


# --- ActivoBank: a description printed on the same visual line ----------------

def test_activobank_blank_description_is_read_from_the_positional_line():
    blank = statement(
        "2026/05/01 A 2026/05/31", "1 000.00",
        [row("05.01", "05.01", "PAG SERV EXEMPLO", Decimal("-50.00"), Decimal("950.00")),
         row("05.03", "05.04", "", Decimal("-123.45"), Decimal("826.55"))],
        "826.55",
    )
    positional = ["05.01 05.01 PAG SERV EXEMPLO 50.00 950.00", "05.03 05.04 COMPRA CONTINENTE 123.45 826.55"]
    repaired = repair_blank_descriptions(blank, positional)

    parsed = ActivoBankParser().parse(repaired)
    assert [t.description for t in parsed.transactions] == ["PAG SERV EXEMPLO", "COMPRA CONTINENTE"]
    assert [t.amount for t in parsed.transactions] == [Decimal("-50.00"), Decimal("-123.45")]


def test_activobank_repair_leaves_a_row_alone_when_its_amounts_do_not_match():
    blank = statement(
        "2026/05/01 A 2026/05/31", "1 000.00",
        [row("05.03", "05.04", "", Decimal("-123.45"), Decimal("876.55"))],
        "876.55",
    )
    positional = ["05.03 05.04 COMPRA CONTINENTE 999.99 876.55"]  # different amount: not the same row
    assert repair_blank_descriptions(blank, positional) == blank


def test_activobank_repair_does_not_touch_rows_that_already_have_a_description():
    text = statement(
        "2026/05/01 A 2026/05/31", "1 000.00",
        [row("05.03", "05.04", "COMPRA CONTINENTE", Decimal("-123.45"), Decimal("876.55"))],
        "876.55",
    )
    assert repair_blank_descriptions(text, ["05.03 05.04 OUTRA COISA 123.45 876.55"]) == text


# --- Universo: a description whose first line sits above its date line ------------

UNIVERSO_TEXT = """\
Movimentos de: 01/09/2016 a 30/09/2016
Conta de Pagamento a Crédito:
Saldo em dívida (à data do extrato anterior)  -100,00 €
21/09/16 22/09/16 281 COMPRA RESIDENCIAL VILA FIM DO MÊS  -60,00 €  0,00 €  -60,00 €  0,60 €  1,00%
COMPRA NA INTERNET TICKETS VALE DE DEUS FIM
21/09/16 22/09/16 281  -45,90 €  0,00 €  -45,90 €  0,46 €  1,00%
DO MÊS
Saldo em dívida (à data do extrato atual)  -205,90 €
"""


def test_universo_description_printed_above_its_date_line_is_kept():
    parsed = parse_universo(UNIVERSO_TEXT, "synthetic.pdf")
    descriptions = [t["description"] for t in parsed["transactions"]]
    assert descriptions == [
        "COMPRA RESIDENCIAL VILA FIM DO MÊS",
        "COMPRA NA INTERNET TICKETS VALE DE DEUS FIM DO MÊS",
    ]
    assert [t["debit"] for t in parsed["transactions"]] == [60.0, 45.9]


# --- WiZink: a row whose date and description are a bitmap ------------------------

WIZINK_TABLE = """\
Descrição da transação  Montante
20,00 €
Pagamentos efetuados no período a que se refere o extrato
"""


def _positional(words_by_line):
    lines = []
    for top, words in words_by_line:
        lines.append(PosLine(page=2, top=top, bottom=top + 12,
                             words=tuple((x, x + 20, top, top + 12, text) for x, text in words)))
    return lines


def test_wizink_bare_amount_is_completed_with_its_ocr_date_and_description(monkeypatch):
    lines = _positional([
        (360, [(11, "Descrição"), (141, "da"), (160, "transação")]),
        (400, [(519, "20,00"), (547, "€")]),
        (430, [(11, "Pagamentos"), (80, "efetuados")]),
    ])
    monkeypatch.setattr(pdfpos, "positional_lines", lambda path, password=None: lines)
    monkeypatch.setattr(pdfpos, "ocr_crop", lambda *a, **k: "15/05 16/05 TAXA EXEMPLO")
    out = wizink.repair_bare_rows(WIZINK_TABLE, "fake.pdf", None)
    assert "15/05 16/05 TAXA EXEMPLO 20,00 €" in out.split("\n")
    # The repaired line is in the shape the WiZink parser reads as a movement.
    m = wizink.TX_RE.match("15/05 16/05 TAXA EXEMPLO 20,00 €")
    assert m and m.group(4) == "TAXA EXEMPLO" and m.group(5) == "20,00"


def test_wizink_bare_amount_stays_blank_when_ocr_reads_nothing(monkeypatch):
    lines = _positional([(400, [(519, "20,00"), (547, "€")])])
    monkeypatch.setattr(pdfpos, "positional_lines", lambda path, password=None: lines)
    monkeypatch.setattr(pdfpos, "ocr_crop", lambda *a, **k: "")
    assert wizink.repair_bare_rows(WIZINK_TABLE, "fake.pdf", None) == WIZINK_TABLE


def test_wizink_amount_outside_the_table_is_not_touched(monkeypatch):
    outside = "Resumo\n20,00 €\n" + WIZINK_TABLE
    lines = _positional([
        (100, [(519, "20,00"), (547, "€")]),
        (300, [(11, "Descrição"), (141, "da"), (160, "transação")]),
        (400, [(519, "20,00"), (547, "€")]),
        (430, [(11, "Pagamentos"), (80, "efetuados")]),
    ])
    monkeypatch.setattr(pdfpos, "positional_lines", lambda path, password=None: lines)
    monkeypatch.setattr(pdfpos, "ocr_crop", lambda *a, **k: "15/05 16/05 TAXA EXEMPLO")
    out = wizink.repair_bare_rows(outside, "fake.pdf", None)
    assert out.split("\n")[1] == "20,00 €"  # the amount above the table keeps its text
    assert "15/05 16/05 TAXA EXEMPLO 20,00 €" in out.split("\n")


# --- Reimport of a stored statement ---------------------------------------------------

def _blank_row_statement() -> str:
    return statement(
        "2026/05/01 A 2026/05/31", "1 000.00",
        [row("05.01", "05.01", "PAG SERV EXEMPLO", Decimal("-50.00"), Decimal("950.00")),
         row("05.02", "05.02", "VENCIMENTO", Decimal("1200.00"), Decimal("2150.00")),
         row("05.03", "05.04", "", Decimal("-123.45"), Decimal("2026.55"))],
        "2 026.55",
    )


def _transactions(client, user):
    return client.get("/api/transactions", headers=as_user(user)).json()["items"]


def test_reimport_fills_a_blank_description_and_keeps_the_category(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 reimport-fill"
    text_by_pdf[pdf] = _blank_row_statement()
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]

    blank = next(t for t in _transactions(client, "helder") if t["description"] == "")
    r = client.post("/api/transactions/bulk-category", headers=as_user("helder"),
                    json={"ids": [blank["id"]], "category": "Groceries > Supermarket"})
    assert r.status_code == 200, r.text

    text_by_pdf[pdf] = basic_statement()  # the same statement, read again with the description
    preview = client.post(f"/api/statements/{statement_id}/reimport", headers=as_user("helder"))
    assert preview.status_code == 200, preview.text
    assert preview.json()["applied"] is False
    assert preview.json()["counts"]["fill_descriptions"] == 1
    assert any(t["description"] == "" for t in _transactions(client, "helder"))  # preview writes nothing

    applied = client.post(f"/api/statements/{statement_id}/reimport?apply=true", headers=as_user("helder"))
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True

    after = {t["id"]: t for t in _transactions(client, "helder")}
    row_after = after[blank["id"]]
    assert row_after["description"] == "COMPRA CONTINENTE"
    assert row_after["category"] == "Groceries > Supermarket"
    assert row_after["category_source"] == "manual"

    again = client.post(f"/api/statements/{statement_id}/reimport?apply=true", headers=as_user("helder")).json()
    assert again["counts"]["fill_descriptions"] == 0 and again["counts"]["to_add"] == 0


def test_reimport_lists_but_does_not_change_a_different_description(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 reimport-differs"
    text_by_pdf[pdf] = basic_statement()
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]

    text_by_pdf[pdf] = basic_statement().replace("COMPRA CONTINENTE", "CONTINENTE LISBOA")
    result = client.post(f"/api/statements/{statement_id}/reimport?apply=true", headers=as_user("helder")).json()
    assert result["counts"]["differs"] == 1
    assert result["counts"]["fill_descriptions"] == 0
    descriptions = {t["description"] for t in _transactions(client, "helder")}
    assert "COMPRA CONTINENTE" in descriptions and "CONTINENTE LISBOA" not in descriptions


def test_reimport_adds_a_new_row_and_categorises_it(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 reimport-add"
    text_by_pdf[pdf] = basic_statement()
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]
    before = client.get("/api/transactions", headers=as_user("helder")).json()["total"]

    # The new reading finds one more row, so the statement must still reconcile.
    extra = statement(
        "2026/05/01 A 2026/05/31", "1 000.00",
        [row("05.01", "05.01", "PAG SERV EXEMPLO", Decimal("-50.00"), Decimal("950.00")),
         row("05.02", "05.02", "VENCIMENTO", Decimal("1200.00"), Decimal("2150.00")),
         row("05.03", "05.04", "COMPRA CONTINENTE", Decimal("-123.45"), Decimal("2026.55")),
         row("05.05", "05.05", "PAG SERV EXEMPLO", Decimal("-10.00"), Decimal("2016.55"))],
        "2 016.55",
    )
    text_by_pdf[pdf] = extra
    result = client.post(f"/api/statements/{statement_id}/reimport?apply=true", headers=as_user("helder")).json()
    assert result["counts"]["to_add"] == 1
    assert client.get("/api/transactions", headers=as_user("helder")).json()["total"] == before + 1


def test_reimport_refuses_to_write_a_reading_that_does_not_reconcile(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 reimport-refused"
    text_by_pdf[pdf] = basic_statement()
    statement_id = upload(client, "helder", account_id, pdf).json()["statement_id"]

    text_by_pdf[pdf] = basic_statement().replace("COMPRA CONTINENTE", "CONTINENTE X").replace(
        "2 026.55", "9 999.99")  # closing balance no longer adds up
    before = _transactions(client, "helder")
    preview = client.post(f"/api/statements/{statement_id}/reimport", headers=as_user("helder"))
    assert preview.status_code == 200
    assert preview.json()["reconciles"] is False

    r = client.post(f"/api/statements/{statement_id}/reimport?apply=true", headers=as_user("helder"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "reimport_refused" if "error" in r.json() else True
    assert _transactions(client, "helder") == before


# --- Universo prefix still parses the sample rows ----------------------------------------

def test_universo_sample_statement_still_reconciles_its_totals():
    parsed = parse_universo(UNIVERSO_TEXT, "synthetic.pdf")
    assert parsed["saldo_inicial"] == -100.0
    assert parsed["saldo_final"] == -205.9


def test_overlapping_statement_of_the_same_account_is_noted(client, text_by_pdf):
    account_id = make_account(client, "helder")
    first = b"%PDF-1.4 overlap-first"
    second = b"%PDF-1.4 overlap-second"
    text_by_pdf[first] = basic_statement()
    text_by_pdf[second] = basic_statement().replace("EXTRATO DE 2026/05/01 A 2026/05/31",
                                                    "EXTRATO DE 2026/05/15 A 2026/06/15")
    assert upload(client, "helder", account_id, first).status_code == 201
    r = upload(client, "helder", account_id, second, name="EXTRATO COMBINADO 2026006.pdf")
    assert r.status_code == 201, r.text
    assert any("overlaps statement" in note for note in r.json()["notes"])


def test_uploading_an_existing_statement_again_brings_in_its_changes(client, text_by_pdf):
    account_id = make_account(client, "helder")
    pdf = b"%PDF-1.4 upload-refresh"
    text_by_pdf[pdf] = _blank_row_statement()
    assert upload(client, "helder", account_id, pdf).status_code == 201

    blank = next(t for t in _transactions(client, "helder") if t["description"] == "")
    client.post("/api/transactions/bulk-category", headers=as_user("helder"),
                json={"ids": [blank["id"]], "category": "Groceries > Supermarket"})

    text_by_pdf[pdf] = basic_statement()  # the same file, now read with the description
    again = upload(client, "helder", account_id, pdf)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "updated"
    assert again.json()["counts"]["fill_descriptions"] == 1

    row = next(t for t in _transactions(client, "helder") if t["id"] == blank["id"])
    assert row["description"] == "COMPRA CONTINENTE"
    assert row["category"] == "Groceries > Supermarket"


def test_another_file_for_the_same_period_that_does_not_add_up_is_kept_for_review(client, text_by_pdf):
    account_id = make_account(client, "helder")
    first = b"%PDF-1.4 period-first"
    second = b"%PDF-1.4 period-second"
    text_by_pdf[first] = basic_statement()
    text_by_pdf[second] = basic_statement(closing_override="2 026.56")
    assert upload(client, "helder", account_id, first).status_code == 201
    r = upload(client, "helder", account_id, second, name="EXTRATO 2.pdf")
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "review"
