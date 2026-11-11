"""Credit card statements, parsed by the card parsers in `cards/`.

Those parsers were verified statement by statement in the previous importer. They return
floats and ISO date strings; this adapter converts them to Decimal and dates, and gives each
card its institution id.

Card statements print no running balance. For cards, the balance on each row is the amount
owed after that row, worked out from the opening balance. Universo prints its own running
balance, so its printed balances are kept.

Card payments from the bank account are left out on purpose by the card parsers: the same
cash movement already appears in the bank statement, so counting it here would double it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .base import ParsedStatement, Transaction
from .cards.affinity import parse_affinity
from .cards.cetelem import parse_cetelem
from .cards.cofidis import parse_cofidis
from .cards.universo import parse_universo
from .cards.wizink import parse_wizink


class CardParseError(ValueError):
    pass


def _money(value) -> Decimal | None:
    return None if value is None else Decimal(str(round(float(value), 2)))


class LegacyCardParser:
    def __init__(self, institution: str, parse_fn, printed_balances: bool):
        self.institution = institution
        self._parse_fn = parse_fn
        self._printed_balances = printed_balances

    def _run(self, text: str) -> dict | None:
        try:
            return self._parse_fn(text, "")
        except Exception:  # a text this parser does not understand is not its statement
            return None

    @staticmethod
    def _is_statement(result: dict | None) -> bool:
        # A statement either has charges, or its table is printed with balances and
        # no charges (for example, a month where the only movement was a payment).
        if not result:
            return False
        if result.get("transactions"):
            return True
        return result.get("saldo_inicial") is not None and result.get("saldo_final") is not None

    def can_parse(self, text: str) -> bool:
        return self._is_statement(self._run(text))

    def parse(self, text: str) -> ParsedStatement:
        result = self._run(text)
        if not self._is_statement(result):
            raise CardParseError(f"{self.institution}: no transaction table found")

        opening = _money(result.get("saldo_inicial"))
        closing = _money(result.get("saldo_final"))
        owed = opening if opening is not None else Decimal("0.00")

        transactions: list[Transaction] = []
        for raw in result["transactions"]:
            booked = date.fromisoformat(raw["lanc_date"])
            value = date.fromisoformat(raw["valor_date"]) if raw.get("valor_date") else booked
            amount = _money((raw.get("credit") or 0.0) - (raw.get("debit") or 0.0))
            if self._printed_balances and raw.get("balance") is not None:
                balance = _money(raw["balance"])
            else:
                # Cards: a purchase (negative amount) raises what is owed.
                # Universo (bank direction) has no printed balance on some rows: a credit raises it.
                owed = owed + amount if self._printed_balances else owed - amount
                balance = owed
            transactions.append(Transaction(
                booked_on=booked, value_date=value, description=raw["description"][:255],
                amount=amount, balance=balance,
            ))

        dates = [t.booked_on for t in transactions]
        if result.get("period_start") and result.get("period_end"):
            start = date.fromisoformat(result["period_start"])
            end = date.fromisoformat(result["period_end"])
        elif dates:
            start, end = min(dates), max(dates)
        else:
            raise CardParseError(f"{self.institution}: statement has no period")

        return ParsedStatement(
            institution=self.institution,
            account_ref=str(result.get("account_number") or ""),
            holder_label=str(result.get("account_name") or ""),
            currency=result.get("currency") or "EUR",
            period_start=start,
            period_end=end,
            opening_balance=opening,
            closing_balance=closing,
            transactions=transactions,
            anomalies=list(result.get("anomalies") or []),
            balance_direction="bank" if self._printed_balances else "card",
        )


CARD_PARSERS = [
    LegacyCardParser("affinity", parse_affinity, printed_balances=False),
    LegacyCardParser("wizink", parse_wizink, printed_balances=False),
    LegacyCardParser("cofidis", parse_cofidis, printed_balances=False),
    LegacyCardParser("universo", parse_universo, printed_balances=True),
    LegacyCardParser("cetelem", parse_cetelem, printed_balances=False),
]
