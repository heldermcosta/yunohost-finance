from datetime import date
from decimal import Decimal

from app.parsers import find_parser
from app.parsers.base import reconcile

SAMPLE = """\
Bankinter, S.A. – Sucursal em Portugal
EXTRACTO DE MOVIMENTOS
Período a que se referem as informações prestadas no presente extrato: de 2025/04/01 a 2025/04/30
CONTA MAIS ORD Nº 0269 0116 1234 5678 90
   Data Descritivo                                 Data     Movimento     Saldo
       Saldo em 2025/04/01                                              0,00
 03/04 Transf imediata de helder costa            03/04       500,00      500,00
 10/04 Iva                                        10/04       -20,70      479,30
 17/04 Trf a credito sepa+ - x - wizink paym      17/04       -11.200,00  -10.720,70
       Saldo em 2025/04/30                                          -10.720,70
EMPRÉSTIMOS E CRÉDITOS
       Saldo em 2025/04/30                                          270.000,00
"""


def test_detects_and_parses_rows_with_portuguese_amounts():
    parser = find_parser(SAMPLE)
    assert parser.institution == "bankinter"
    st = parser.parse(SAMPLE)
    assert st.period_start == date(2025, 4, 1)
    assert st.period_end == date(2025, 4, 30)
    assert st.opening_balance == Decimal("0.00")
    assert st.closing_balance == Decimal("-10720.70")
    assert [t.amount for t in st.transactions] == [
        Decimal("500.00"), Decimal("-20.70"), Decimal("-11200.00"),
    ]
    assert st.transactions[2].booked_on == date(2025, 4, 17)
    assert reconcile(st).status == "ok"


def test_wizink_payment_line_is_not_claimed_by_card_parser():
    assert find_parser(SAMPLE).institution == "bankinter"
