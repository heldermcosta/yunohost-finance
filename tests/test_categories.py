from decimal import Decimal

from app.categories import DEFAULT_CATEGORIES, SEPARATOR, categorize, in_category


def test_default_tree_includes_subcategories_and_no_duplicates():
    assert f"Housing & Utilities{SEPARATOR}Water" in DEFAULT_CATEGORIES
    assert "Housing & Utilities" in DEFAULT_CATEGORIES
    assert len(DEFAULT_CATEGORIES) == len(set(DEFAULT_CATEGORIES))


def test_keywords_pick_the_subcategory():
    assert categorize("PAG AGUAS DO TEJO", Decimal("-30"))[0] == f"Housing & Utilities{SEPARATOR}Water"
    assert categorize("EDP COMERCIAL", Decimal("-60"))[0] == f"Housing & Utilities{SEPARATOR}Electricity"
    assert categorize("GALP GAS NATURAL", Decimal("-40"))[0] == f"Housing & Utilities{SEPARATOR}Gas"
    assert categorize("GALP 2345 POSTO", Decimal("-50"))[0] == f"Transport{SEPARATOR}Fuel"
    assert categorize("UBER EATS LISBOA", Decimal("-12"))[0] == f"Dining Out{SEPARATOR}Delivery"


def test_in_category_matches_parent_and_children_only():
    assert in_category("Housing & Utilities", "Housing & Utilities")
    assert in_category(f"Housing & Utilities{SEPARATOR}Water", "Housing & Utilities")
    assert not in_category("Housing & UtilitiesX", "Housing & Utilities")
