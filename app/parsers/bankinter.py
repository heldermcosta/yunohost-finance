"""Bankinter (Portugal) monthly "EXTRACTO" account statements.

Layout facts (from the statements):
- Header "Período a que se referem ... de yyyy/mm/dd a yyyy/mm/dd".
- The account line reads "CONTA MAIS ORD Nº <number>" (or another account name).
- The movement table is headed "EXTRACTO DE MOVIMENTOS" / "CONTAS À ORDEM".
- Rows: "dd/mm <descrição> dd/mm <movimento> <saldo>". Dates have no year.
- Amounts use Portuguese formatting: "9.500,00", "-133.847,31".
- The movement column is already signed (credit +, debit -).
- "Saldo em yyyy/mm/dd" opens the table (period start) and closes it (period end).
  The first closing line ends the table; loan sections printed later are ignored.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from .base import ParsedStatement, Transaction

INSTITUTION = "bankinter"

PERIOD_RE = re.compile(
    r"de\s+(\d{4})/(\d{2})/(\d{2})\s+a\s+(\d{4})/(\d{2})/(\d{2})"
)
ACCOUNT_RE = re.compile(r"(CONTA[A-Z ]*?)\s+N[º°]\s*(\d[\d ]*\d)")
MONEY = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
MONEY_RE = re.compile(MONEY)
ROW_RE = re.compile(
    r"^\s*(\d{2}/\d{2})\s+(.+?)\s+(\d{2}/\d{2})\s+(" + MONEY + r")\s+(" + MONEY + r")\s*$"
)
SALDO_RE = re.compile(r"^\s*Saldo em (\d{4})/(\d{2})/(\d{2})\b")


def _money(s: str) -> Decimal:
    return Decimal(s.replace(".", "").replace(",", "."))


def _day_month(md: str, start: date, end: date) -> date:
    day_s, month_s = md.split("/")
    month, day = int(month_s), int(day_s)
    year = end.year if (start.year != end.year and month < start.month) else start.year
    return date(year, month, day)


class BankinterParser:
    institution = INSTITUTION

    def can_parse(self, text: str) -> bool:
        return "EXTRACTO DE MOVIMENTOS" in text.upper() and "BANKINTER" in text.upper()

    def parse(self, text: str) -> ParsedStatement:
        period = PERIOD_RE.search(text)
        if not period:
            raise ValueError("Bankinter: no 'de yyyy/mm/dd a yyyy/mm/dd' period found")
        start = date(int(period.group(1)), int(period.group(2)), int(period.group(3)))
        end = date(int(period.group(4)), int(period.group(5)), int(period.group(6)))

        acc = ACCOUNT_RE.search(text)
        holder_label = acc.group(1).strip() if acc else ""
        account_ref = re.sub(r"\s", "", acc.group(2)) if acc else ""

        opening: Decimal | None = None
        closing: Decimal | None = None
        rows: list[Transaction] = []
        anomalies: list[str] = []
        in_table = False

        for line in text.split("\n"):
            saldo = SALDO_RE.match(line)
            if saldo:
                when = date(int(saldo.group(1)), int(saldo.group(2)), int(saldo.group(3)))
                found = MONEY_RE.findall(line)
                if not found:
                    continue
                if opening is None and not in_table and when == start:
                    opening = _money(found[-1])
                    in_table = True
                    continue
                if in_table and when == end:
                    closing = _money(found[-1])
                    break
                continue

            if not in_table:
                continue

            m = ROW_RE.match(line)
            if not m:
                continue
            booked = _day_month(m.group(1), start, end)
            value = _day_month(m.group(3), start, end)
            description = re.sub(r"\s{2,}", " ", m.group(2).strip())
            amount = _money(m.group(4))
            balance = _money(m.group(5))
            rows.append(
                Transaction(
                    booked_on=booked,
                    value_date=value,
                    description=description,
                    amount=amount,
                    balance=balance,
                )
            )

        if opening is None or closing is None:
            raise ValueError("Bankinter: 'Saldo em' opening or closing line not found")

        return ParsedStatement(
            institution=INSTITUTION,
            account_ref=account_ref,
            holder_label=holder_label,
            currency="EUR",
            period_start=start,
            period_end=end,
            opening_balance=opening,
            closing_balance=closing,
            transactions=rows,
            anomalies=anomalies,
        )
