"""Parser registry. Each institution parser is tried in order on the statement text."""

from __future__ import annotations

from .activobank import ActivoBankParser
from .base import StatementParser
from .cards_legacy import CARD_PARSERS

PARSERS: list[StatementParser] = [
    ActivoBankParser(),
    *CARD_PARSERS,
]


class NoParserError(LookupError):
    pass


def find_parser(text: str) -> StatementParser:
    for parser in PARSERS:
        if parser.can_parse(text):
            return parser
    raise NoParserError("no installed parser recognises this statement")
