"""Parser registry. Each institution parser is tried in order on the statement text."""

from __future__ import annotations

from .activobank import ActivoBankParser
from .bankinter import BankinterParser
from .base import StatementParser
from .cards_legacy import CARD_PARSERS

# Bank layouts first: some bank statements mention card brands (for example a
# WiZink payment line on a Bankinter statement), and the card parsers must not
# claim them.
PARSERS: list[StatementParser] = [
    ActivoBankParser(),
    BankinterParser(),
    *CARD_PARSERS,
]


INSTITUTION_NAMES = {
    "activobank": "ActivoBank", "bankinter": "Bankinter", "affinity": "Affinity",
    "wizink": "WiZink", "cofidis": "Cofidis", "universo": "Universo", "cetelem": "Cetelem",
}


class NoParserError(LookupError):
    pass


def find_parser(text: str) -> StatementParser:
    for parser in PARSERS:
        if parser.can_parse(text):
            return parser
    raise NoParserError("no installed parser recognises this statement")
