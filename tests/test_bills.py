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

    places = {p["name"]: p for p in summary["places"]}
    assert places["Lisboa"]["house"] is True
    assert places["Lisboa"]["utilities"]["electricity"][1] == 52.42
    assert 1 not in places["Lisboa"]["missing"]["electricity"]          # February has its bill
    assert places["Lisboa"]["missing"]["electricity"] == [m for m in range(12) if m != 1]  # the other months are gaps

    grid = client.get("/api/bills/grid", headers=as_user("helder")).json()
    lisboa = next(g for g in grid if g["name"] == "Lisboa")
    assert lisboa["utilities"] == ["electricity"]
    assert lisboa["years"][0]["months"] == {"electricity": [None, 52.42] + [None] * 10}
    assert lisboa["years"][0]["items"]["electricity"][1][0]["has_pdf"] is True

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


NOS = """NOS Comunicações, S.A. Sede: Rua Exemplo, 1600-404 Lisboa.
N.º cliente     N.º contribuinte
C844000000      230000000
Fatura n.º      Data da fatura       Período de faturação
FT 202293/1747833      26-09-2022     Setembro de 2022
EXMO. SR.
HELDER MIGUEL ALMEIDA COSTA
AV EXEMPLO Nº 9 5º E
2910 - 383 SETÚBAL
Valor desta fatura com IVA                     €60,49
Total a pagar até 19 out 2022                  €60,49
"""

NOS_WITHOUT_ADDRESS = """NOS Comunicações, S.A. Sede: Rua Exemplo, 1600-404 Lisboa.
Fatura n.º      Data da fatura
FT 202490/1331600      03-05-2024
O seu período de faturação inicia ao dia 1 de cada mês.
Nesta fatura podem estar incluídos consumos adicionais de 01-05-2024 até 31-05-2024.
Valor desta fatura com IVA                     €49,99
Total a pagar até 28 jun 2024                  €49,99
"""

NOWO = """NOWO Communications, S.A.
A sua factura nowo FACTURA/RECIBO
N° Factura Data
R11037587 03-11-2025
Morada de Instalação
RUA EXEMPLO, 1C
2910-719 SETÚBAL
RESUMO DESTA FACTURA Valor (C/IVA) E VALOR A PAGAR
VALOR DESTA FACTURA C/IVA € 20,00
Detalhe da Factura Qtd Periodo de facturagao Valor (c/ IVA)
NET120 1 de 01/11/25 a 30/11/25 € 27,50
"""

VODAFONE = """Vodafone Portugal, Comunicações Pessoais, S.A. Sede: Avenida Exemplo.
HELDER MIGUEL ALMEIDA COSTA
R LUIS EXEMPLO LT 19 31 6 C
1750-159 LISBOA
FT 001/017130125     230000000     310000000
Data de emissão: 20 ago 2016
Período de faturação: 16 jul a 15 ago
Montante: € 26,89
Total com IVA € 26,89
"""

VODAFONE_STATEMENT = """Vodafone Easy
Extrato de Serviço jul 2016
Helder Costa
R EXEMPLO 112
Período: 5 jul a 4 set
Este documento não serve de fatura. Deve ser utilizado para efeitos exclusivamente informativos.
Valor a pagar € 10,00
"""


def test_nos_bill_is_read():
    bill = read_bill_texts(NOS, NOS)
    assert bill["utility"] == "telecom" and bill["provider"] == "NOS"
    assert bill["invoice"] == "FT 202293/1747833"
    assert bill["issued"] == date(2022, 9, 26)
    assert bill["period_end"] == date(2022, 9, 30)  # no "até" date here: the named month ends
    assert bill["amount"] == 60.49
    assert bill["address"] == "AV EXEMPLO"  # the house number is cut with the other columns
    assert bill["status"] == "ok"


def test_nos_bill_without_printed_address_is_kept_for_review():
    bill = read_bill_texts(NOS_WITHOUT_ADDRESS, NOS_WITHOUT_ADDRESS)
    assert bill["provider"] == "NOS" and bill["amount"] == 49.99
    assert bill["period_end"] == date(2024, 5, 31)  # the "até" date, not the month of issue
    assert bill["address"] is None and bill["status"] == "review"


def test_nowo_bill_is_read_with_its_billing_period():
    bill = read_bill_texts(NOWO, NOWO)
    assert bill["utility"] == "telecom" and bill["provider"] == "NOWO"
    assert bill["invoice"] == "R11037587"
    assert bill["issued"] == date(2025, 11, 3)
    assert bill["period_start"] == date(2025, 11, 1) and bill["period_end"] == date(2025, 11, 30)
    assert bill["amount"] == 20.00
    assert bill["address"] == "RUA EXEMPLO, 1C"
    assert bill["status"] == "ok"


def test_vodafone_bill_is_read_from_its_first_page():
    bill = read_bill_texts(VODAFONE, VODAFONE)
    assert bill["provider"] == "Vodafone" and bill["invoice"] == "FT 001/017130125"
    assert bill["issued"] == date(2016, 8, 20)
    assert bill["period_end"] == date(2016, 8, 15)
    assert bill["amount"] == 26.89
    assert bill["address"] == "R LUIS EXEMPLO LT 19 31 6 C"
    assert bill["status"] == "ok"


EPAL = """FATURA nº 201700000001, emitida em 2017.04.10
Código de Cliente
19000000
Valor a Pagar 12.71 €
Débito a partir de 2017.05.05
Resumo da FATURA nº 201700000001
Período de Faturação de 2017.03.08 a 2017.04.06
EPAL - Abastecimento de Água
Morada Postal (Principal) - R EXEMPLO DE MACEDO LOTE 1 6 C
Referências para Alteração ou Cancelamento do Débito Direto SEPA
"""


def test_epal_water_bill_is_read_despite_its_direct_debit_text():
    bill = read_bill_texts(EPAL, EPAL)
    assert bill["utility"] == "water" and bill["provider"] == "EPAL"
    assert bill["invoice"] == "201700000001"
    assert bill["issued"] == date(2017, 4, 10)
    assert bill["period_start"] == date(2017, 3, 8) and bill["period_end"] == date(2017, 4, 6)
    assert bill["amount"] == 12.71
    assert bill["address"] == "R EXEMPLO DE MACEDO LOTE 1 6 C"
    assert bill["status"] == "ok"


AGUAS_2022 = """FATURA Nº
202200000001
Data de Emissão :
22-11-2022
Matriculada na Conservatória do Registo Comercial de Setúbal sob o número 504014889
DADOS DO CONTRATO
Titular:
HELDER MIGUEL ALMEIDA COSTA
230795870
Local de consumo :
HELDER MIGUEL ALMEIDA COSTA
AV ALENTEJO (DO)
9 - 05 ESQ
SETÚBAL
TOTAL A PAGAR
8,92 €
"""

AGUAS_2019 = """FATURA nº 201900000002
Tel: 265 549 300 - NIPC 504 014 889
Data de Emissão: 2019.05.23
Período de Faturação:
18ABR2019 a 17MAI2019
HELDER MIGUEL ALMEIDA COSTA
AV ALENTEJO (DO)
9 - 05 ESQ
TOTAL A PAGAMENTO  €8.19
"""


def test_aguas_do_sado_bill_without_a_printed_period_is_kept_for_review():
    bill = read_bill_texts(AGUAS_2022, AGUAS_2022)
    assert bill["provider"] == "Águas do Sado" and bill["utility"] == "water"
    assert bill["invoice"] == "202200000001" and bill["issued"] == date(2022, 11, 22)
    assert bill["amount"] == 8.92 and bill["period_end"] is None
    assert bill["address"] == "AV ALENTEJO (DO) 9-05 ESQ"
    assert bill["status"] == "review"


AGUAS_COMERCIAL = """FATURA Nº
202200000003
Data de Emissão :
22-11-2022
Matriculada na Conservatória do Registo Comercial de Setúbal sob o número 504014889
Local de consumo :

HELDER MIGUEL ALMEIDA COSTA
230795870
COM E IND
000000003396599
DN 15 mm|2,5 m3 / h
RUA ROBERTO IVENS
3- SETÚBAL
RUA MOUZINHO DE ALBUQUERQUE
TOTAL A PAGAR
11,58 €
"""


def test_aguas_do_sado_commercial_bill_takes_its_supply_address_not_its_postal_one():
    bill = read_bill_texts(AGUAS_COMERCIAL, AGUAS_COMERCIAL)
    assert bill["amount"] == 11.58 and bill["invoice"] == "202200000003"
    assert bill["address"] == "RUA ROBERTO IVENS 3"


def test_aguas_do_sado_bill_with_a_printed_period_is_read():
    bill = read_bill_texts(AGUAS_2019, AGUAS_2019)
    assert bill["invoice"] == "201900000002" and bill["issued"] == date(2019, 5, 23)
    assert bill["period_start"] == date(2019, 4, 18) and bill["period_end"] == date(2019, 5, 17)
    assert bill["amount"] == 8.19
    assert bill["address"] == "AV ALENTEJO (DO) 9-05 ESQ"
    assert bill["status"] == "ok"


def test_vodafone_statement_is_not_a_bill():
    assert read_bill_texts(VODAFONE_STATEMENT, VODAFONE_STATEMENT) is None


def test_month_end_and_short_dates():
    assert bills._month_end("Setembro de 2022") == date(2022, 9, 30)
    assert bills._month_end("Fevereiro de 2024") == date(2024, 2, 29)
    assert bills._month_end("Dezembro de 2025") == date(2025, 12, 31)
    assert bills._short_date("01/11/25") == date(2025, 11, 1)
