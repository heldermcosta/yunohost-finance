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
