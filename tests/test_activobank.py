"""ActivoBank parser tests on synthetic statements.

The statements are generated in the same column layout as the real ones
(DEBITO / CREDITO / SALDO header ends), with invented names and numbers.
No real statement data is used anywhere in the tests.
"""

from datetime import date
from decimal import Decimal

from app.parsers import find_parser, NoParserError
from app.parsers.activobank import ActivoBankParser
from app.parsers.base import reconcile

DEBITO_END = 60
CREDITO_END = 75
SALDO_END = 90


def fmt(value: Decimal) -> str:
    return f"{abs(value):,.2f}".replace(",", " ")


def header_line() -> str:
    buf = [" "] * SALDO_END
    for word, end in (("DEBITO", DEBITO_END), ("CREDITO", CREDITO_END), ("SALDO", SALDO_END)):
        buf[end - len(word):end] = list(word)
    return "".join(buf).rstrip()


def row_at(booked: str, value: str, desc: str, amount_text: str, col_end: int, saldo: Decimal) -> str:
    left = f"{booked}  {value}  {desc}"
    assert len(left) + len(amount_text) < col_end, "description too long for test layout"
    line = left.ljust(col_end - len(amount_text)) + amount_text
    saldo_text = fmt(saldo)
    return line.ljust(SALDO_END - len(saldo_text)) + saldo_text


def row(booked: str, value: str, desc: str, amount: Decimal, saldo: Decimal) -> str:
    col_end = DEBITO_END if amount < 0 else CREDITO_END
    return row_at(booked, value, desc, fmt(amount), col_end, saldo)


def statement(period: str, opening: str, rows: list[str], closing: str) -> str:
    lines = [
        "ACTIVOBANK TESTE",
        f"EXTRATO DE {period}",
        "HELDER TESTE N. 123456 MOEDA: EUR",
        "",
        header_line(),
        f"SALDO INICIAL{' ' * 40}{opening}",
        *rows,
        f"SALDO FINAL{' ' * 42}{closing}",
    ]
    return "\n".join(lines) + "\n"


def basic_statement(closing_override: str | None = None, balance_override: Decimal | None = None) -> str:
    # Rows print dates as MM.DD, so these are 1, 2 and 3 May 2026.
    rows = [
        row("05.01", "05.01", "PAG SERV EXEMPLO", Decimal("-50.00"), Decimal("950.00")),
        row("05.02", "05.02", "VENCIMENTO", Decimal("1200.00"), balance_override or Decimal("2150.00")),
        row("05.03", "05.04", "COMPRA CONTINENTE", Decimal("-123.45"), Decimal("2026.55")),
    ]
    return statement(
        "2026/05/01 A 2026/05/31",
        "1 000.00",
        rows,
        closing_override or "2 026.55",
    )


def test_detects_activobank_statement():
    parser = find_parser(basic_statement())
    assert isinstance(parser, ActivoBankParser)


def test_unknown_document_is_not_recognised():
    try:
        find_parser("Some unrelated document")
    except NoParserError:
        return
    raise AssertionError("unrelated text should not match a parser")


def test_parses_header_and_transactions():
    st = ActivoBankParser().parse(basic_statement())

    assert st.institution == "activobank"
    assert st.account_ref == "123456"
    assert st.currency == "EUR"
    assert st.period_start == date(2026, 5, 1)
    assert st.period_end == date(2026, 5, 31)
    assert st.opening_balance == Decimal("1000.00")
    assert st.closing_balance == Decimal("2026.55")

    assert [t.amount for t in st.transactions] == [
        Decimal("-50.00"),
        Decimal("1200.00"),
        Decimal("-123.45"),
    ]
    assert st.transactions[0].booked_on == date(2026, 5, 1)
    assert st.transactions[0].value_date == date(2026, 5, 1)
    assert st.transactions[2].value_date == date(2026, 5, 4)
    assert st.transactions[2].description == "COMPRA CONTINENTE"
    assert st.transactions[2].balance == Decimal("2026.55")


def test_correct_statement_reconciles():
    st = ActivoBankParser().parse(basic_statement())
    result = reconcile(st)
    assert result.ok
    assert result.difference == Decimal("0.00")
    assert result.row_warnings == []


def test_wrong_closing_balance_fails_reconciliation():
    st = ActivoBankParser().parse(basic_statement(closing_override="2 026.56"))
    result = reconcile(st)
    assert result.status == "failed"
    assert result.difference == Decimal("0.01")


def test_wrong_running_balance_on_a_row_is_a_warning_not_a_failure():
    # Totals still agree, so the statement is kept, but it goes to review.
    st = ActivoBankParser().parse(basic_statement(balance_override=Decimal("2150.01")))
    result = reconcile(st)
    assert result.status == "warning"
    assert result.difference == Decimal("0.00")
    assert len(result.row_warnings) == 2  # the bad row and the row after it


def test_sign_follows_running_balance_when_column_is_misaligned():
    # The second movement is a debit (balance falls by 20.00) but its amount
    # is printed under the CREDITO column. The balance decides the sign.
    text = statement(
        "2026/05/01 A 2026/05/31",
        "1 000.00",
        [
            row("05.01", "05.01", "PAG SERV EXEMPLO", Decimal("-50.00"), Decimal("950.00")),
            row_at("05.02", "05.02", "COMPRA TESTE", "20.00", CREDITO_END, Decimal("930.00")),
        ],
        "930.00",
    )
    st = ActivoBankParser().parse(text)
    assert st.transactions[1].amount == Decimal("-20.00")
    assert any("taken from running balance" in a for a in st.anomalies)
    assert reconcile(st).ok


def test_year_rollover_uses_period_years():
    text = statement(
        "2025/12/01 A 2026/01/31",
        "500.00",
        [
            row("12.20", "12.20", "COMPRA TESTE", Decimal("-10.00"), Decimal("490.00")),
            row("01.05", "01.05", "COMPRA TESTE", Decimal("-5.00"), Decimal("485.00")),  # 5 Jan 2026
        ],
        "485.00",
    )
    st = ActivoBankParser().parse(text)
    assert st.transactions[0].booked_on == date(2025, 12, 20)
    assert st.transactions[1].booked_on == date(2026, 1, 5)
    assert reconcile(st).ok


def test_rows_after_saldo_final_are_ignored():
    # A row that appears after the movement table (e.g. an embedded card sub-statement).
    extra = row("20.05", "20.05", "LINHA FORA DA TABELA", Decimal("-999.00"), Decimal("1.00"))
    text = basic_statement() + extra + "\n"
    st = ActivoBankParser().parse(text)
    assert len(st.transactions) == 3
    assert reconcile(st).ok


def test_trailing_minus_is_a_negative_balance():
    from app.parsers.activobank import _money
    assert _money("62 809.11-") == Decimal("-62809.11")
    assert _money("1 341.46") == Decimal("1341.46")
