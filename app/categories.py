"""Keyword-based categories. Rules are data, not code, so they can be edited per user later.

The first rule whose keyword appears in the upper-cased description wins, so put
specific merchants before generic words. The rules here are a starting set; the
full rule list from the previous importer will be ported with its tests.
"""

from __future__ import annotations

from decimal import Decimal

UNCATEGORISED = "Uncategorised"

RULES: list[tuple[str, list[str]]] = [
    ("Card Rewards / Cashback", ["BONIFICAÇÃO", "BONIFICACAO"]),
    ("Mortgage", ["EMPRESTIMO HIPOT", "CREDITO HABITACAO", "PAG.PRESTACAO"]),
    ("Insurance", ["SEGURO", "OCIDENTAL", "FIDELIDADE", "ASISA", "REAL VIDA"]),
    ("Consumer Credit / Credit Cards", ["WIZINK", "COFIDIS", "UNICRE", "CETELEM", "AFFINITY", "UNIVERSO"]),
    ("Housing & Utilities", ["EDP", "GALP", "AGUAS DO", "VODAFONE", "NOS COMUNICACO", "MEO/TMN"]),
    ("Groceries", ["CONTINENTE", "PINGO DOCE", "MERCADONA", "LIDL", "ALDI"]),
    ("Salary / Income", ["VENCIMENTO"]),
    ("Transfers - Personal", ["TRF", "TRANSFERENCIA", "MB WAY"]),
]


def categorize(description: str, amount: Decimal | None = None) -> str:
    text = (description or "").upper()
    for category, keywords in RULES:
        if any(k in text for k in keywords):
            return category
    return UNCATEGORISED
