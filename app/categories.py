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


DEFAULT_CATEGORIES = [
    "Groceries", "Housing & Utilities", "Mortgage", "Insurance", "Consumer Credit / Credit Cards",
    "Salary / Income", "Card Rewards / Cashback", "Transfers - Personal",
    "Dining Out", "Transport", "Health", "Shopping", "Entertainment", "Subscriptions",
    "Taxes", "Bank Fees", "Cash", UNCATEGORISED,
]


def categorize(description: str, amount: Decimal | None = None,
               user_rules: list[tuple[str, str]] | None = None) -> tuple[str, str]:
    """Returns (category, source). Source is "rule" for a saved rule, "default" for built-in keywords."""
    text = (description or "").upper()
    for contains, category in user_rules or []:
        if contains.upper() in text:
            return category, "rule"
    for category, keywords in RULES:
        if any(k in text for k in keywords):
            return category, "default"
    return UNCATEGORISED, "default"
