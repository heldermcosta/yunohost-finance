from datetime import date
from decimal import Decimal

from app.duplicates import StatementMovements, find_duplicate_statements, movement_signature, repeated_amounts

from tests.test_app import as_user, client, make_account, settings, text_by_pdf  # noqa: F401


def sig(day, amount, text, balance="100.00"):
    return movement_signature(date.fromisoformat(day), amount, text, balance)


def stmt(id, account, start, end, *signatures):
    return StatementMovements(id, account, date.fromisoformat(start), date.fromisoformat(end),
                              f"file{id}.pdf", frozenset(signatures))


def test_a_statement_stored_twice_is_flagged_with_the_money_it_repeats():
    rows = [sig("2026-05-02", "-30.00", "PINGO DOCE", "70.00"), sig("2026-05-10", "1000.00", "SALARIO", "1070.00")]
    pairs = find_duplicate_statements([stmt(1, 7, "2026-05-01", "2026-05-31", *rows),
                                       stmt(2, 7, "2026-05-01", "2026-05-31", *rows)])
    assert len(pairs) == 1 and pairs[0]["identical"] and pairs[0]["shared"] == 2
    assert repeated_amounts(pairs[0]) == (Decimal("1000.00"), Decimal("30.00"))


def test_a_recurring_charge_in_different_months_is_not_a_duplicate():
    fee = sig("2026-05-07", "-1.00", "COMISSAO SERVICO", "1.00")
    other = sig("2026-06-07", "-1.00", "COMISSAO SERVICO", "1.00")
    assert find_duplicate_statements([stmt(1, 7, "2026-05-01", "2026-05-31", fee),
                                      stmt(2, 7, "2026-06-01", "2026-06-30", other)]) == []


def test_statements_of_different_accounts_or_periods_are_not_compared():
    rows = [sig("2026-05-02", "-30.00", "PINGO DOCE")]
    assert find_duplicate_statements([stmt(1, 7, "2026-05-01", "2026-05-31", *rows),
                                      stmt(2, 8, "2026-05-01", "2026-05-31", *rows)]) == []
    assert find_duplicate_statements([stmt(1, 7, "2026-04-01", "2026-04-30", *rows),
                                      stmt(2, 7, "2026-05-01", "2026-05-31", *rows)]) == []


def test_a_partial_overlap_is_flagged_only_above_the_share_threshold():
    common = [sig(f"2026-05-0{i}", "-10.00", f"SHOP {i}") for i in range(1, 5)]
    extra = [sig("2026-05-20", "-10.00", "SHOP X")]
    assert find_duplicate_statements([stmt(1, 7, "2026-05-01", "2026-05-31", *common, *extra),
                                      stmt(2, 7, "2026-05-01", "2026-05-31", *common)]) != []  # 4 of 4 shared
    other = [sig("2026-05-25", "-10.00", "SHOP Y"), sig("2026-05-26", "-10.00", "SHOP Z")]
    assert find_duplicate_statements([stmt(1, 7, "2026-05-01", "2026-05-31", *common[:2], *other),
                                      stmt(2, 7, "2026-05-01", "2026-05-31", *common[:2], *extra)]) == []  # 2 of 4 shared


def test_duplicates_endpoint_is_empty_for_a_clean_database(client):
    make_account(client, "helder")
    r = client.get("/api/duplicates", headers=as_user("helder"))
    assert r.status_code == 200, r.text
    assert r.json() == {"count": 0, "income_twice": "0", "spending_twice": "0", "pairs": []}
