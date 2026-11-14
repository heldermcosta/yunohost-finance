"""Utility bills. Bill text is synthetic (invented addresses and amounts); PDF extraction is replaced by that text."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from app import bills
from app.bills import fold, house_for, read_bill_texts
from tests.test_app import as_user, client, settings, text_by_pdf  # noqa: F401  (fixtures)


EDP = """EDP Comercial - Comercialização de Energia, S.A.
Documento emitido a: 1 de março 2018
Período de faturação: 31 de janeiro a 28 de fevereiro 2018
Quanto tenho
a pagar?
52,42 €
FATURA Nº 10213867688
Morada de fornecimento
RUA EXEMPLO 28 6 C
1750-159 LISBOA
"""

GALP = """Galp Power, S.A.
Fatura: FT 0001/103115783
Data: 03 JUL 2018                  Período de Faturação: 01 JUN 2018 a 02 JUL 2018
O valor desta fatura é
12,73 €
Local de fornecimento
RUA EXEMPLO 7 LOTE 2
LISBOA
"""

SMS_WATER = """SERVIÇOS MUNICIPALIZADOS DE SETÚBAL - www.sms-setubal.pt
FATURA Nº: FT 20243/30200447
Data de emissão: 19-04-2024
Local de Consumo: RUA EXEMPLO 3
Período de Faturação 30 dias de 03-04-2024 a 02-05-2024
Total conta cliente
9,29 €
Água
"""

PALMELA_WATER = """Este documento foi enviado por cm-palmela.pt
Data de Emissão 15-01-2026
Último Período de Faturação 12-12-2025 a 10-01-2026
MORADA DE FORNECIMENTO
Rua Exemplo,Lote: U, nº9, N/A - Palmela
Total (€):
17,71
Fatura n.º FAC 002/2686765
Água m3
"""


def test_edp_bill_is_read():
    bill = read_bill_texts(EDP, EDP)
    assert bill["utility"] == "electricity" and bill["provider"] == "EDP"
    assert bill["amount"] == 52.42
    assert bill["period_end"] == date(2018, 2, 28)
    assert bill["issued"] == date(2018, 3, 1)
    assert bill["invoice"] == "10213867688"
    assert bill["address"] == "RUA EXEMPLO 28 6 C"
    assert bill["status"] == "ok"


def test_galp_gas_bill_is_read():
    bill = read_bill_texts(GALP, GALP)
    assert bill["utility"] == "gas" and bill["provider"] == "Galp"
    assert bill["amount"] == 12.73
    assert bill["period_end"] == date(2018, 7, 2)
    assert bill["issued"] == date(2018, 7, 3)
    assert bill["address"] == "RUA EXEMPLO 7 LOTE 2"


def test_sms_water_bill_is_read():
    bill = read_bill_texts(SMS_WATER, SMS_WATER)
    assert bill["utility"] == "water" and bill["provider"] == "SMS Setúbal"
    assert bill["amount"] == 9.29
    assert bill["period_end"] == date(2024, 5, 2)
    assert bill["address"] == "RUA EXEMPLO 3"
    assert bill["invoice"] == "FT 20243/30200447"


def test_palmela_water_bill_is_read():
    bill = read_bill_texts(PALMELA_WATER, PALMELA_WATER)
    assert bill["provider"] == "Câmara de Palmela"
    assert bill["amount"] == 17.71
    assert bill["period_end"] == date(2026, 1, 10)
    assert bill["invoice"] == "FAC 002/2686765"


def test_address_is_cut_before_the_next_column():
    text = "EDP Comercial\nQuanto tenho\na pagar?\n10,00 €\nDocumento emitido a: 1 de março 2018\n" \
           "Período de faturação: 1 de janeiro a 31 de janeiro 2018\n" \
           "Morada de fornecimento\nRUA EXEMPLO 1      NIF 123\n"
    assert read_bill_texts(text, text)["address"] == "RUA EXEMPLO 1"


def test_bill_without_a_street_address_is_kept_for_review():
    text = EDP.replace("RUA EXEMPLO 28 6 C", "xxxxxxxx")
    bill = read_bill_texts(text, text)
    assert bill is not None and bill["status"] == "review" and bill["address"] is None


@pytest.mark.parametrize("text", [
    "AUTORIZAÇÃO DE DÉBITO DIRETO SEPA CORE\nEDP Comercial\nDocumento emitido a: 1 de março 2018",  # SEPA mandate
    "Caro(a) Cliente,\nEDP Comercial\nAtendimento a clientes 808 53 53 53",                       # letter
    "Local de Consumo: RUA EXEMPLO 3\nÁgua\nLeaflet about the water service",                       # leaflet, no amount
    "Galp Power\nDocumentosContrato\nCondições Particulares de Fornecimento",                      # contract pack
    "Extrato de conta\nSaldo final 1.234,56 €",                                                    # a bank statement
])
def test_non_bills_are_not_read(text):
    assert read_bill_texts(text, text) is None


def test_house_match_ignores_case_and_accents():
    houses = [("Lisboa", "Pastor de Macedo"), ("Setúbal", "AV. Alentejo 9")]
    assert house_for("RUA LUÍS PASTOR DE MACEDO 28", houses) == "Lisboa"
    assert house_for("Av. Alentejo 9 5 Esq", houses) == "Setúbal"
    assert house_for("RUA ROBERTO IVENS 3", houses) is None


def test_small_words_and_punctuation_do_not_block_a_match():
    houses = [("Lisboa", "Pastor Macedo"), ("Setúbal", "Av. Alentejo (do) 9")]
    assert house_for("RUA LUÍS PASTOR DE MACEDO 28", houses) == "Lisboa"
    assert house_for("AV ALENTEJO 9 ESQ 5", houses) == "Setúbal"
    assert house_for("AV ALENTEJO 90", houses) is None   # 9 is a whole word, not a prefix of 90


def test_first_matching_house_wins():
    houses = [("Primeira", "RUA EXEMPLO"), ("Segunda", "EXEMPLO 3")]
    assert house_for("RUA EXEMPLO 3", houses) == "Primeira"
    assert fold("  rua  Exemplo ") == "RUA EXEMPLO"


@pytest.fixture
def bill_texts(monkeypatch):
    """Maps fake PDF bytes to (raw, layout) text."""
    texts: dict[bytes, tuple[str, str]] = {}

    def fake(pdf_bytes, work_dir):
        return texts.get(pdf_bytes, ("", ""))

    monkeypatch.setattr(bills, "_pdf_texts", fake)
    return texts


def upload_file(client, user, name, data):
    return client.post("/api/statements", headers=as_user(user),
                       files={"file": (name, data, "application/pdf" if data.startswith(b"%PDF") else "application/zip")})


def add_house(client, user, name, keyword):
    r = client.post("/api/bill-houses", headers=as_user(user), json={"name": name, "keyword": keyword})
    assert r.status_code == 201, r.text
    return r.json()


def test_bill_upload_summary_and_delete(client, bill_texts):
    pdf = b"%PDF-1.4 electricity june"
    bill_texts[pdf] = (EDP, EDP)
    add_house(client, "helder", "Lisboa", "EXEMPLO 28")

    r = upload_file(client, "helder", "0317.pdf", pdf)
    assert r.status_code == 201, r.text
    assert r.json() == {"status": "bills", "added": 1, "already": 0, "review": 0, "skipped": 0, "file": "0317.pdf"}

    again = upload_file(client, "helder", "0317 copy.pdf", pdf)
    assert again.json()["already"] == 1 and again.json()["added"] == 0

    summary = client.get("/api/bills/summary", headers=as_user("helder")).json()
    assert summary["year"] == 2018 and summary["years"] == [2018]
    lisboa = next(h for h in summary["houses"] if h["name"] == "Lisboa")
    assert lisboa["months"][1] == 52.42 and lisboa["total"] == 52.42   # February: the period end month

    rows = client.get("/api/bills", headers=as_user("helder"), params={"year": 2018, "house": "Lisboa"}).json()
    assert len(rows) == 1 and rows[0]["invoice"] == "10213867688" and rows[0]["utility"] == "electricity"

    bill_id = rows[0]["id"]
    assert client.delete(f"/api/bills/{bill_id}", headers=as_user("helder")).status_code == 200
    assert client.get("/api/bills", headers=as_user("helder"), params={"year": 2018}).json() == []


def test_unassigned_locations_are_listed_until_a_house_is_added(client, bill_texts):
    pdf = b"%PDF-1.4 gas"
    bill_texts[pdf] = (GALP, GALP)
    upload_file(client, "helder", "gas.pdf", pdf)
    summary = client.get("/api/bills/summary", headers=as_user("helder")).json()
    assert summary["unassigned"][0]["location"] == "RUA EXEMPLO 7 LOTE 2"
    assert summary["unassigned"][0]["total"] == 12.73

    add_house(client, "helder", "Setúbal", "EXEMPLO 7")
    summary = client.get("/api/bills/summary", headers=as_user("helder")).json()
    assert summary["unassigned"] == []
    assert next(h for h in summary["houses"] if h["name"] == "Setúbal")["total"] == 12.73


def test_zip_of_bills_is_stored_and_non_bills_are_counted(client, bill_texts):
    edp, gas, leaflet = b"%PDF-1.4 edp", b"%PDF-1.4 gas", b"%PDF-1.4 leaflet"
    bill_texts[edp] = (EDP, EDP)
    bill_texts[gas] = (GALP, GALP)
    bill_texts[leaflet] = ("Leaflet", "Leaflet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Lisboa/Eletricidade/0317.pdf", edp)
        z.writestr("Lisboa/Gás/0313.pdf", gas)
        z.writestr("Lisboa/Água/folheto.pdf", leaflet)
        z.writestr("Lisboa/notas.txt", "not a pdf")
    r = client.post("/api/statements", headers=as_user("helder"),
                    files={"file": ("bills.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 201, r.text
    assert r.json()["added"] == 2 and r.json()["skipped"] == 2
    assert len(client.get("/api/bills", headers=as_user("helder"), params={"year": 2018}).json()) == 2


def test_a_second_copy_of_the_same_invoice_is_not_stored_twice(client, bill_texts):
    first, copy = b"%PDF-1.4 copy one", b"%PDF-1.4 copy two"   # same invoice, different bytes and name
    bill_texts[first] = (EDP, EDP)
    bill_texts[copy] = (EDP, EDP)
    assert upload_file(client, "helder", "0328.pdf", first).json()["added"] == 1
    again = upload_file(client, "helder", "0329.pdf", copy).json()
    assert again["added"] == 0 and again["already"] == 1
    assert len(client.get("/api/bills", headers=as_user("helder"), params={"year": 2018}).json()) == 1


def test_bills_belong_to_their_owner(client, bill_texts):
    pdf = b"%PDF-1.4 owned"
    bill_texts[pdf] = (EDP, EDP)
    upload_file(client, "helder", "0317.pdf", pdf)
    assert client.get("/api/bills", headers=as_user("maria"), params={"year": 2018}).json() == []
    assert client.get("/api/bills/summary", headers=as_user("maria")).json()["houses"] == []


def test_review_bills_are_listed_separately(client, bill_texts):
    pdf = b"%PDF-1.4 no address"
    bill_texts[pdf] = (EDP.replace("RUA EXEMPLO 28 6 C", "xxxxxxxx"), EDP.replace("RUA EXEMPLO 28 6 C", "xxxxxxxx"))
    upload_file(client, "helder", "masked.pdf", pdf)
    review = client.get("/api/bills", headers=as_user("helder"), params={"review": "true"}).json()
    assert len(review) == 1 and review[0]["status"] == "review"
    assert client.get("/api/bills/summary", headers=as_user("helder")).json()["review"] == 1


def test_a_non_bill_pdf_goes_the_statement_way(client, bill_texts, text_by_pdf):  # noqa: F811
    pdf = b"%PDF-1.4 bank"
    bill_texts[pdf] = ("EXTRATO DE CONTA\nSaldo final 1.234,56 €", "EXTRATO DE CONTA")
    text_by_pdf[pdf] = "EXTRATO DE CONTA\nSaldo final 1.234,56 €"
    r = upload_file(client, "helder", "extrato.pdf", pdf)
    assert r.status_code == 422  # no statement parser knows this text, so it is refused as a statement
    assert client.get("/api/bills", headers=as_user("helder"), params={"year": 2018}).json() == []
