"""Card statements. Text is synthetic and follows the layout the card parsers read."""

from decimal import Decimal

from app.parsers import find_parser
from app.parsers.base import reconcile

AFFINITY_TEXT = """
Cartão Affinity - EXTRATO
Saldo Inicial 100,00 €
05-03-2026 1234 PAGAMENTO POR MULTIBANCO 50,00 €
06-03-2026 1234 LOJA EXEMPLO 20,00 €
Total 170,00 €
"""


def test_card_statement_is_recognised_and_reconciles():
    parser = find_parser(AFFINITY_TEXT)
    assert parser.institution == "affinity"
    statement = parser.parse(AFFINITY_TEXT)
    assert statement.balance_direction == "card"
    # The payment line is left out on purpose: it is already a debit in the bank statement.
    assert [t.description for t in statement.transactions] == ["LOJA EXEMPLO"]
    assert statement.transactions[0].amount == Decimal("-20.00")
    assert reconcile(statement).status == "ok"


def test_card_purchase_raises_what_is_owed():
    statement = find_parser(AFFINITY_TEXT).parse(AFFINITY_TEXT)
    assert statement.transactions[-1].balance == Decimal("170.00")


def test_bank_payment_to_a_card_company_is_not_spending():
    from decimal import Decimal as D
    from app.categories import is_card_payment
    assert is_card_payment("PAG SERV WIZINK", D("-120.00"), "activobank")
    assert not is_card_payment("PAG SERV WIZINK", D("120.00"), "activobank")  # a refund is not a payment
    assert not is_card_payment("CONTINENTE", D("-20.00"), "activobank")


def test_affinity_statement_with_only_a_payment_is_accepted():
    # A month whose only movement is the payment of the previous balance: no charges,
    # so the parser must still recognise the statement and take its period from the heading.
    from app.parsers import find_parser
    from app.parsers.base import reconcile

    text = "\n".join([
        "Extrato nº 77",
        "Saldo em dívida à data do extrato anterior          45,29 €",
        "MASTERCARD - Detalhe dos movimentos de 14-03-2025 a 13-04-2025",
        "  Data Valor   Cartão   Descritivo   Moeda   Valor",
        "04-04-2025  TIT  Pagamento por DD  -45,29 €",
        "Total                                              0,00 €",
        "Todas as compras efetuadas com o cartão nas lojas da rede INDITEX em Portugal têm uma bonificação de 2%.",
    ])
    parser = find_parser(text)
    assert parser.institution == "affinity"
    st = parser.parse(text)
    assert st.transactions == []
    assert st.period_start.isoformat() == "2025-03-14"
    assert st.period_end.isoformat() == "2025-04-13"
    assert reconcile(st).status == "ok"


def test_cetelem_2026_rows_with_two_digit_years_are_read():
    from app.parsers import find_parser
    from app.parsers.base import reconcile

    text = "\n".join([
        "Período Extrato Atual:         17/08/2026 a 16/09/2026",
        "Detalhes de Movimento do Período de Extrato (valores em €)",
        "17/08/26    17/08/26    VALOR EM DIVIDA FECHO EXTRATO ANTERIOR    -    -    -    -    -    100,00",
        "20/08/26    20/08/26    AUCHAN SETUBAL II    2,83    -    -    -    -    10,00    110,00",
        "Valores Antecipados (valores em €)",
    ])
    parser = find_parser(text)
    assert parser.institution == "cetelem"
    st = parser.parse(text)
    assert len(st.transactions) == 1
    assert st.transactions[0].amount == Decimal("-10.00")
    assert st.transactions[0].booked_on.isoformat() == "2026-08-20"
    assert reconcile(st).status == "ok"


def test_cofidis_account_is_the_dossier_not_the_reissued_card_number():
    from app.parsers.cards.cofidis import parse_cofidis

    def statement(card):
        return "\n".join([
            "Nº de Dossier",
            "50000005140553",
            "Nº de Cartão",
            f"XXXXXXXXXX{card}",
            "Período de extrato de 09/12/2020 a 07/01/2021",
            "Descritivo",
        ])

    # Same customer, two cards: the account must be the dossier so both land in one account.
    old = parse_cofidis(statement("13865X"), "a.pdf")
    new = parse_cofidis(statement("30720X"), "b.pdf")
    assert old["account_number"] == new["account_number"] == "50000005140553"
