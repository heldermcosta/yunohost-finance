"""Keyword-based categories. Rules are data, not code, so they can be edited per user later.

The first rule whose keyword appears in the upper-cased description wins, so put
specific merchants before generic words. The rules here are a starting set; the
full rule list from the previous importer will be ported with its tests.
"""

from __future__ import annotations

from decimal import Decimal

UNCATEGORISED = "Uncategorised"

# A subcategory is stored as "Parent > Child". The text is the whole name, so
# rules, budgets and exports need no separate column.
SEPARATOR = " > "

# Default tree: parent -> subcategories. Parents without children are simple categories.
DEFAULT_TREE: dict[str, list[str]] = {
    "Groceries": ["Supermarket", "Other Food"],
    "Dining Out": ["Restaurants", "Coffee & Snacks", "Delivery"],
    "Housing & Utilities": ["Water", "Electricity", "Gas", "Internet & Phone", "Rent", "Home Maintenance"],
    "Transport": ["Fuel", "Public Transport", "Parking & Tolls", "Taxi & Ride-hailing", "Car Maintenance"],
    "Health": ["Pharmacy", "Doctor & Clinic", "Dentist"],
    "Shopping": ["Clothing", "Electronics", "Home & Garden", "Online"],
    "Entertainment": ["Streaming", "Events & Leisure"],
    "Subscriptions": ["Software & Apps", "Memberships"],
    "Travel": ["Flights", "Hotels", "Car Rental"],
    "Insurance": ["Car", "Health", "Home", "Life"],
    "Taxes": ["IRS", "IMI", "IUC"],
    "Salary / Income": ["Salary", "Other Income"],
    "Mortgage": [],
    "Consumer Credit / Credit Cards": [],
    "Card Rewards / Cashback": [],
    "Transfers - Personal": [],
    "Bank Fees": [],
    "Cash": [],
    "Card payments": [],
}

DEFAULT_CATEGORIES = [
    name
    for parent, children in DEFAULT_TREE.items()
    for name in [parent] + [f"{parent}{SEPARATOR}{child}" for child in children]
] + [UNCATEGORISED]

# Keyword rules. The first rule whose keyword appears in the upper-cased description
# wins, so specific subcategories come before their parents and before generic words.
RULES: list[tuple[str, list[str]]] = [
    # Moves into or out of the GoBulling trading sub-account are your own money.
    ("Transfers - Personal", ["GOBULLING"]),
    ("Card Rewards / Cashback", ["BONIFICAÇÃO", "BONIFICACAO"]),
    ("Mortgage", ["EMPRESTIMO HIPOT", "CREDITO HABITACAO", "PAG.PRESTACAO"]),
    ("Insurance", ["SEGURO", "OCIDENTAL", "FIDELIDADE", "ASISA", "REAL VIDA"]),
    ("Consumer Credit / Credit Cards", ["WIZINK", "COFIDIS", "UNICRE", "CETELEM", "AFFINITY", "UNIVERSO"]),
    (f"Housing & Utilities{SEPARATOR}Water", ["AGUAS DO", "AGUAS DE", "ÁGUAS", "EPAL", "SIMAR", "AQUAPOR"]),
    (f"Housing & Utilities{SEPARATOR}Electricity", ["EDP", "ENDESA", "IBERDROLA", "GOLDENERGY"]),
    (f"Housing & Utilities{SEPARATOR}Gas", ["GALP GAS", "NATURGY", "GAS NATURAL", "BUTANO"]),
    (f"Housing & Utilities{SEPARATOR}Internet & Phone", ["VODAFONE", "NOS COMUNICACO", "MEO/TMN", "NOWO", "DIGI MOBIL"]),
    (f"Groceries{SEPARATOR}Supermarket", ["CONTINENTE", "PINGO DOCE", "MERCADONA", "LIDL", "ALDI", "AUCHAN", "INTERMARCHE", "E.LECLERC"]),
    (f"Dining Out{SEPARATOR}Delivery", ["UBER EATS", "GLOVO", "BOLT FOOD", "JUST EAT"]),
    (f"Dining Out{SEPARATOR}Coffee & Snacks", ["STARBUCKS", "PASTELARIA", "CAFE", "CAFÉ"]),
    (f"Dining Out{SEPARATOR}Restaurants", ["RESTAURANTE", "RESTAURANT"]),
    (f"Transport{SEPARATOR}Fuel", ["GALP", "REPSOL", "BP PORTUGAL", "PRIO", "CEPSA"]),
    (f"Transport{SEPARATOR}Public Transport", ["COMBOIOS DE PORTUGAL", "METRO LISBOA", "CARRIS", "TRANSTEJO", "SOFLUSA", "ANDANTE"]),
    (f"Transport{SEPARATOR}Parking & Tolls", ["VIA VERDE", "EMEL", "ESTACIONAMENTO", "PORTAGEM"]),
    (f"Transport{SEPARATOR}Taxi & Ride-hailing", ["UBER", "BOLT", "FREE NOW"]),
    (f"Health{SEPARATOR}Pharmacy", ["FARMACIA", "FARMÁCIA"]),
    (f"Entertainment{SEPARATOR}Streaming", ["NETFLIX", "SPOTIFY", "DISNEY", "AMAZON PRIME", "YOUTUBE"]),
    (f"Taxes{SEPARATOR}IRS", ["AUTORIDADE TRIBUTARIA", "IRS"]),
    (f"Taxes{SEPARATOR}IMI", ["IMPOSTO MUNICIPAL"]),
    ("Housing & Utilities", ["VODAFONE", "NOS COMUNICACO", "MEO/TMN"]),
    ("Groceries", ["CONTINENTE", "PINGO DOCE", "MERCADONA", "LIDL", "ALDI"]),
    ("Salary / Income", ["VENCIMENTO"]),
    ("Transfers - Personal", ["TRF", "TRANSFERENCIA", "MB WAY"]),
]


def parent_of(category: str) -> str:
    return category.split(SEPARATOR, 1)[0]


def in_category(name: str, category: str) -> bool:
    """True when name is the category itself or one of its subcategories."""
    return name == category or name.startswith(category + SEPARATOR)


# Bank debits that pay a card company. The card statement already lists the purchases,
# so these payments are kept out of spending (they are marked as transfers).
CARD_COMPANY_HINTS = ("AFFINITY", "WIZINK", "COFIDIS", "CETELEM", "UNIVERSO")


def is_card_payment(description: str, amount: Decimal, bank_institution: str) -> bool:
    return (bank_institution == "activobank" and amount < 0
            and any(h in (description or "").upper() for h in CARD_COMPANY_HINTS))


def rule_object(description: str, user_rules: list | None = None) -> str | None:
    """The object of the first saved rule that matches, if that rule sets one."""
    text = (description or "").upper()
    for contains, _category, *rest in user_rules or []:
        if contains.upper() in text:
            return rest[0] if rest and rest[0] else None
    return None


def categorize(description: str, amount: Decimal | None = None,
               user_rules: list[tuple[str, str]] | None = None) -> tuple[str, str]:
    """Returns (category, source). Source is "rule" for a saved rule, "default" for built-in keywords."""
    text = (description or "").upper()
    for contains, category, *_ in user_rules or []:
        if contains.upper() in text:
            return category, "rule"
    for category, keywords in RULES:
        if any(k in text for k in keywords):
            return category, "default"
    return UNCATEGORISED, "default"
