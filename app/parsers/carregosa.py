"""Banco Carregosa monthly "Resumo Integrado" reports.

Each monthly report summarises one client account. What is imported:
- The "Depósitos à Ordem" sub-account: its movements become transactions and its
  opening and closing balances are reconciled like a bank account.
- "Total Ativos" (everything held, including the trading sub-account) is stored
  as a snapshot.

The daily "Relatório de Lançamentos" (RL) reports repeat the same cash movements
and are not imported, so nothing is counted twice.

Layout facts (from the reports):
- Period: "Resumo Integrado de yyyy-mm-dd a yyyy-mm-dd".
- Account: "Conta n.º: 6158838".
- Movement rows: "yyyy-mm-dd  yyyy-mm-dd  <descrição>  <montante com sinal>  <saldo>".
- The block starts at "Moeda: EUR / Depósitos à Ordem" and ends at its "Saldo Final".
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from .base import ParsedStatement, Transaction

INSTITUTION = "carregosa"
# Thousands may be written with a dot ("5.851,70") or a space ("3 030,79").
MONEY = r"-?\d{1,3}(?:[ .]\d{3})*,\d{2}"
PERIOD_RES = [
    re.compile(r"Resumo\s+Integrado\s+de\s+(\d{4}-\d{2}-\d{2})\s+a\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE),
    re.compile(r"Per[ií]odo:\s*(\d{4}-\d{2}-\d{2})\s+a\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE),
]
ACCOUNT_RE = re.compile(r"Conta\s+n\.º:\s*(\d+)")
TOTAL_ATIVOS_RE = re.compile(r"Total\s+Ativos\s+(" + MONEY + ")")
# Where the cash movements of the "Depósitos à Ordem" account start: "Moeda: EUR / Dep. Ordem"
# (2017), "Moeda: EUR / Depósitos à Ordem" (2019) or a "Depósitos à Ordem - EUR" heading (2025+).
MOVEMENTS_HEADER_RE = re.compile(r"Extrato\s+de\s+Movimentos|Movimentos\s+Dep", re.IGNORECASE)
SECTION_RE = re.compile(
    r"^\s*(?:Moeda:\s*EUR\s*/\s*)?Dep(?:ósitos|\.)\s+(?:à\s+)?Ordem(?:\s*-\s*EUR|\s+Transporte)?\s*$",
    re.IGNORECASE,
)
ROW_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s+(" + MONEY + r")\s+(" + MONEY + r")\s*$"
)
SALDO_INICIAL_RE = re.compile(r"^\s*Saldo\s+(?:Contabilístico\s+)?Inicial\s+(" + MONEY + ")")
SALDO_FINAL_RE = re.compile(r"^\s*Saldo\s+(?:Contabilístico\s+)?Final\s+(" + MONEY + ")")


def _money(s: str) -> Decimal:
    return Decimal(s.replace(" ", "").replace(".", "").replace(",", "."))


class CarregosaParser:
    institution = INSTITUTION

    def can_parse(self, text: str) -> bool:
        upper = text.upper()
        return "RESUMO INTEGRADO" in upper and "CARREGOSA" in upper and (
            "DEPÓSITOS À ORDEM" in upper or "DEP. ORDEM" in upper)

    def parse(self, text: str) -> ParsedStatement:
        period = next((m for m in (r.search(text) for r in PERIOD_RES) if m), None)
        if not period:
            raise ValueError("Carregosa: no period found")
        account = ACCOUNT_RE.search(text)
        if not account:
            raise ValueError("Carregosa: no account number found")

        lines = text.split("\n")
        # Only look for the block after the movements heading, so the summary table
        # ("Dep. Ordem 0,00") is not taken for the block.
        heading = next((i for i, line in enumerate(lines) if MOVEMENTS_HEADER_RE.search(line)), None)
        if heading is None:
            raise ValueError("Carregosa: no movements section found")
        start = next((i for i in range(heading, len(lines)) if SECTION_RE.search(lines[i])), None)
        if start is None:
            raise ValueError("Carregosa: no 'Depósitos à Ordem' movement block found")

        rows: list[Transaction] = []
        closing: Decimal | None = None
        explicit_opening: Decimal | None = None
        for line in lines[start + 1:]:
            final = SALDO_FINAL_RE.match(line)
            if final:
                closing = _money(final.group(1))
                break
            initial = SALDO_INICIAL_RE.match(line)
            if initial:
                explicit_opening = _money(initial.group(1))
                continue
            m = ROW_RE.match(line)
            if m:
                rows.append(Transaction(
                    booked_on=date.fromisoformat(m.group(1)),
                    value_date=date.fromisoformat(m.group(2)),
                    description=" ".join(m.group(3).split()),
                    amount=_money(m.group(4)),
                    balance=_money(m.group(5)),
                ))
        if closing is None:
            raise ValueError("Carregosa: 'Saldo Final' of the Depósitos à Ordem block not found")

        # The block may continue from the previous page ("Transporte"), so the opening
        # is the first row's balance before its own movement.
        if explicit_opening is not None:
            opening = explicit_opening
        else:
            opening = rows[0].balance - rows[0].amount if rows else closing
        total = TOTAL_ATIVOS_RE.search(text)

        return ParsedStatement(
            institution=INSTITUTION,
            account_ref=account.group(1),
            holder_label="Banco Carregosa",
            currency="EUR",
            period_start=date.fromisoformat(period.group(1)),
            period_end=date.fromisoformat(period.group(2)),
            opening_balance=opening,
            closing_balance=closing,
            transactions=rows,
            anomalies=[],
            snapshot={
                "total_assets": str(_money(total.group(1))) if total else None,
                "cash": str(closing),
                "term_deposits": [],
            },
        )
