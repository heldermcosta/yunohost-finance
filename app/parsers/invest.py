"""Banco Invest "Posição Integrada" / "Extracto Integrado" monthly reports.

Each report is a snapshot of everything held at a month end. The part that
moves money is the current account ("Depósitos à Ordem"): its movements become
transactions, and its opening and closing balances are reconciled like a bank
statement. The rest (total assets, term deposits) is stored as a snapshot.

Layout facts (from the reports):
- Period: the first "dd de <mês> de yyyy" in the text, e.g. "31 de Outubro de 2012"
  or "31 DE DEZEMBRO DE 2025".
- Client: "Cliente : 754952" (2012-2016) or "Cliente : 754952" in the newer header.
- Movements: "yyyy-mm-dd  yyyy-mm-dd  <descrição>  <débito>  <crédito>  <saldo>".
- "Saldo Anterior: <valor>" opens the table and "Saldo em yyyy-mm-dd : <valor>" closes it.
- Term deposits: "<descrição>  yyyy-mm-dd  yyyy-mm-dd  <TAN>  <montante>  <moeda>".
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal

from .base import ParsedStatement, Transaction

INSTITUTION = "invest"

MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
MONEY = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
PERIOD_RE = re.compile(r"(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)\s+de\s+(\d{4})", re.IGNORECASE)
CLIENT_RE = re.compile(r"Cliente\s*:\s*(\d+)")
SALDO_ANTERIOR_RE = re.compile(r"Saldo\s+Anterior:?\s*(" + MONEY + ")", re.IGNORECASE)
SALDO_EM_RE = re.compile(r"Saldo\s+em\s+\d{4}-\d{2}-\d{2}\s*:?\s*(" + MONEY + ")", re.IGNORECASE)
TOTAL_ACTIVOS_RE = re.compile(r"Total\s+de\s+Activos:?\s*(" + MONEY + ")", re.IGNORECASE)
MOVEMENT_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s{2,}("
    + MONEY + r")\s+(" + MONEY + r")\s+(" + MONEY + r")\s*$"
)
TERM_DEPOSIT_RE = re.compile(
    r"^\s*(.+?)\s{2,}(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})\s+(\d+(?:,\d+)?)\s+(" + MONEY
    + r")\s+([A-Z]{2,3})\s*$"
)


def _money(s: str) -> Decimal:
    return Decimal(s.replace(".", "").replace(",", "."))


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _report_date(text: str) -> date | None:
    for m in PERIOD_RE.finditer(text):
        month = MONTHS.get(_strip_accents(m.group(2)).lower())
        if month:
            return date(int(m.group(3)), month, int(m.group(1)))
    return None


class InvestParser:
    institution = INSTITUTION

    def can_parse(self, text: str) -> bool:
        upper = text.upper()
        return ("BANCO INVEST" in upper or "BANCOINVEST" in upper) and (
            "POSIÇÃO INTEGRADA" in upper or "POSIÇAO INTEGRADA" in upper or "EXTRACTO INTEGRADO" in upper
            or "EXTRATO INTEGRADO" in upper
        ) and "DEPÓSITOS À ORDEM" in upper

    def parse(self, text: str) -> ParsedStatement:
        report_day = _report_date(text)
        if report_day is None:
            raise ValueError("Invest: no report date found")
        client = CLIENT_RE.search(text)
        if not client:
            raise ValueError("Invest: no client number found")

        opening_m = SALDO_ANTERIOR_RE.search(text)
        closing_m = SALDO_EM_RE.search(text)
        opening = _money(opening_m.group(1)) if opening_m else None
        closing = _money(closing_m.group(1)) if closing_m else None

        rows: list[Transaction] = []
        in_table = False
        for line in text.split("\n"):
            if not in_table:
                if SALDO_ANTERIOR_RE.search(line):
                    in_table = True
                continue
            if SALDO_EM_RE.search(line):
                break
            m = MOVEMENT_RE.match(line)
            if not m:
                continue
            debit, credit, balance = _money(m.group(4)), _money(m.group(5)), _money(m.group(6))
            rows.append(Transaction(
                booked_on=date.fromisoformat(m.group(1)),
                value_date=date.fromisoformat(m.group(2)),
                description=" ".join(m.group(3).split()),
                amount=credit - debit,
                balance=balance,
            ))

        total = TOTAL_ACTIVOS_RE.search(text)
        term_deposits = []
        for line in text.split("\n"):
            d = TERM_DEPOSIT_RE.match(line)
            if d:
                term_deposits.append({
                    "description": " ".join(d.group(1).split()),
                    "start": d.group(2), "end": d.group(3),
                    "rate": d.group(4), "amount": str(_money(d.group(5))), "currency": d.group(6),
                })

        return ParsedStatement(
            institution=INSTITUTION,
            account_ref=client.group(1),
            holder_label="Banco Invest",
            currency="EUR",
            period_start=report_day,
            period_end=report_day,
            opening_balance=opening,
            closing_balance=closing,
            transactions=rows,
            anomalies=[],
            snapshot={
                "total_assets": str(_money(total.group(1))) if total else None,
                "cash": str(closing) if closing is not None else None,
                "term_deposits": term_deposits,
            },
        )
