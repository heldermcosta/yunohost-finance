"""ActivoBank "EXTRATO COMBINADO" monthly account statements.

Layout facts (from the statements and the previous importer):
- A header line "EXTRATO DE yyyy/mm/dd A yyyy/mm/dd" gives the period.
- A line "<NAME> N. <number> MOEDA: EUR" gives the account.
- A header line containing DEBITO, CREDITO and SALDO gives the column ends.
  An amount belongs to DEBITO or CREDITO depending on which column end it is
  closest to.
- Movement rows start with two dates (dd.mm lançamento, dd.mm valor) and end
  with the movement amount and the running balance.
- "SALDO INICIAL" and "SALDO FINAL" give the opening and closing balances.
  The first occurrence wins; anything after "SALDO FINAL" (for example an
  embedded card sub-statement) is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .base import ParsedStatement, Transaction

INSTITUTION = "activobank"

# A trailing "-" marks a negative balance (overdraft), e.g. "62 809.11-".
AMT_RE = re.compile(r"-?\d{1,3}(?:\s\d{3})*\.\d{2}-?")
DATE_LEAD_RE = re.compile(r"^\s*(\d{1,2}\.\d{1,2})\s+(\d{1,2}\.\d{1,2})\s")
# A whole movement row read by position: two dates, description, amount, balance.
ROW_TEXT_RE = re.compile(
    r"(?<![\d.])(\d{1,2}\.\d{1,2})\s+(\d{1,2}\.\d{1,2})\s+(.+?)\s+"
    r"(-?\d{1,3}(?:\s\d{3})*\.\d{2}-?)\s+(-?\d{1,3}(?:\s\d{3})*\.\d{2}-?)\s*$",
    re.MULTILINE,
)
EXTRATO_DE_RE = re.compile(
    r"EXTRATO DE\s+(\d{4})/(\d{2})/(\d{2})\s+A\s+(\d{4})/(\d{2})/(\d{2})"
)
ACCOUNT_RE = re.compile(
    r"([A-Z ÇÃÕÁÉÍÓÚÂÊÔ]{4,}?)\s+N\.\s*(\d+)\s+MOEDA:?\s*([A-Z]{3})"
)

# Column end positions used when the header line is not found.
DEFAULT_DEBITO_END = 232
DEFAULT_CREDITO_END = 249


class ActivoBankParseError(ValueError):
    pass


def _money(s: str) -> Decimal:
    negative = s.endswith("-")
    value = Decimal(s.rstrip("-").replace(" ", "").replace("\xa0", ""))
    return -value if negative else value


def _resolve_year(month: int, start: date, end: date) -> int:
    if start.year == end.year:
        return start.year
    if month == start.month:
        return start.year
    if month == end.month:
        return end.year
    return start.year


def _to_date(md: str, start: date, end: date) -> date:
    # Rows print the date as "MM.DD" without a year.
    month_s, day_s = md.split(".")
    month, day = int(month_s), int(day_s)
    year = _resolve_year(month, start, end)
    return date(year, month, day)


def repair_blank_descriptions(layout_text: str, positional_texts: list[str]) -> str:
    """Put back descriptions that the layout text printed on a separate line.

    A movement row whose description sits on another text line comes out of
    `pdftotext -layout` with two empty columns between its dates and its
    amount. The same row, read by position, has its description on the same
    visual line. For each such row, the description is written into the blank
    space of the layout line, character for character, so the column positions
    the parser depends on do not move. A row is only changed if the amounts it
    contains are exactly the same afterwards.
    """
    by_key: dict[tuple[str, str, str, str], list[str]] = {}
    for text in positional_texts:
        for m in ROW_TEXT_RE.finditer(text):
            key = (m.group(1), m.group(2), m.group(4), m.group(5))
            by_key.setdefault(key, []).append(m.group(3).strip())

    out_lines = []
    for line in layout_text.split("\n"):
        dm = DATE_LEAD_RE.match(line)
        matches = list(AMT_RE.finditer(line)) if dm else []
        if not dm or len(matches) != 4:
            out_lines.append(line)
            continue
        if line[matches[1].end():matches[-2].start()].strip():
            out_lines.append(line)  # already has a description
            continue
        key = (dm.group(1), dm.group(2), matches[-2].group(), matches[-1].group())
        candidates = by_key.get(key, [])
        if len(set(candidates)) != 1 or not candidates[0]:
            out_lines.append(line)  # unknown or ambiguous: leave the row as it is
            continue
        start, end = matches[1].end(), matches[-2].start()
        room = end - start - 1
        description = candidates[0][:room]
        filled = (" " + description).ljust(end - start)
        new_line = line[:start] + filled + line[end:]
        if [m.group() for m in AMT_RE.finditer(new_line)] != [m.group() for m in matches]:
            out_lines.append(line)
            continue
        out_lines.append(new_line)
    return "\n".join(out_lines)


class ActivoBankParser:
    institution = INSTITUTION

    def repair_text(self, layout_text: str, pdf_path, password: str | None) -> str:
        """Fill in descriptions printed on a separate line (see repair_blank_descriptions)."""
        from .pdfpos import positional_lines

        positional = [line.text for line in positional_lines(pdf_path, password)]
        return repair_blank_descriptions(layout_text, positional)

    def can_parse(self, text: str) -> bool:
        return EXTRATO_DE_RE.search(text) is not None

    def parse(self, text: str) -> ParsedStatement:
        period = EXTRATO_DE_RE.search(text)
        if not period:
            raise ActivoBankParseError("no 'EXTRATO DE ... A ...' period line found")

        start = date(int(period.group(1)), int(period.group(2)), int(period.group(3)))
        end = date(int(period.group(4)), int(period.group(5)), int(period.group(6)))

        acc = ACCOUNT_RE.search(text)
        holder_label = acc.group(1).strip() if acc else ""
        account_ref = acc.group(2) if acc else ""
        currency = acc.group(3) if acc else "EUR"

        debito_end, credito_end = DEFAULT_DEBITO_END, DEFAULT_CREDITO_END
        opening: Decimal | None = None
        closing: Decimal | None = None
        rows: list[_RawRow] = []
        anomalies: list[str] = []
        prev_balance = Decimal("0.00")
        row_no = 0

        for line in text.split("\n"):
            if closing is not None:
                # Movement table is finished. Ignore anything embedded after it.
                break

            if "DEBITO" in line and "CREDITO" in line and "SALDO" in line:
                debito_end = line.index("DEBITO") + len("DEBITO")
                credito_end = line.index("CREDITO") + len("CREDITO")
                continue

            stripped = line.strip()
            if stripped.startswith("SALDO INICIAL"):
                if opening is None:
                    found = list(AMT_RE.finditer(line))
                    if found:
                        opening = _money(found[-1].group())
                        prev_balance = opening
                continue
            if stripped.startswith("SALDO FINAL"):
                found = list(AMT_RE.finditer(line))
                if found:
                    closing = _money(found[-1].group())
                continue
            if stripped.startswith(("A TRANSPORTAR", "TRANSPORTE", "SALDO DISPONIVEL")):
                continue

            dm = DATE_LEAD_RE.match(line)
            if not dm:
                continue

            row_no += 1
            matches = list(AMT_RE.finditer(line))
            # Two dates + movement amount + running balance.
            if len(matches) < 4:
                anomalies.append(f"row has {len(matches)} numeric fields, expected 4: {line.strip()!r}")
                continue
            if len(matches) > 4:
                anomalies.append(f"row has {len(matches)} numeric fields, using first movement and last balance: {line.strip()!r}")

            description_start = matches[1].end()
            amount_match = matches[-2]
            balance_match = matches[-1]
            description = re.sub(r"\s{2,}", " ", line[description_start:amount_match.start()].strip())

            booked = _to_date(dm.group(1), start, end)
            value = _to_date(dm.group(2), start, end)

            amount = abs(_money(amount_match.group()))
            balance = _money(balance_match.group())
            is_debit = abs(amount_match.end() - debito_end) <= abs(amount_match.end() - credito_end)
            column_sign = -amount if is_debit else amount
            balance_sign = balance - prev_balance  # valid only if |delta| == amount
            prev_balance = balance

            rows.append(
                _RawRow(
                    booked=booked,
                    value=value,
                    description=description,
                    amount=amount,
                    balance=balance,
                    column_signed=column_sign,
                    balance_signed=balance_sign if abs(balance_sign) == amount else None,
                    row_no=row_no,
                )
            )

        if opening is None or closing is None:
            raise ActivoBankParseError("SALDO INICIAL or SALDO FINAL not found")

        # Decide the sign of each movement. The bank's layout (column position)
        # is used first, because it is what the printed total reconciles with.
        # Only if that total does not reconcile is the running balance used to
        # choose the signs, and only if that version does reconcile.
        column_total = sum((r.column_signed for r in rows), Decimal("0.00"))
        if opening + column_total == closing:
            chosen = "column"
        else:
            balance_total = sum(
                (r.balance_signed if r.balance_signed is not None else r.column_signed for r in rows),
                Decimal("0.00"),
            )
            if opening + balance_total == closing:
                chosen = "balance"
                anomalies.append("signs taken from running balance: column layout did not reconcile")
            else:
                chosen = "column"  # keep the layout reading; reconciliation will report the failure

        transactions: list[Transaction] = []
        for r in rows:
            if chosen == "balance" and r.balance_signed is not None:
                signed = r.balance_signed
            elif chosen == "balance":
                signed = r.column_signed
                anomalies.append(f"row {r.row_no}: movement does not match balance change")
            else:
                signed = r.column_signed
            transactions.append(
                Transaction(
                    booked_on=r.booked,
                    value_date=r.value,
                    description=r.description,
                    amount=signed,
                    balance=r.balance,
                )
            )

        return ParsedStatement(
            institution=INSTITUTION,
            account_ref=account_ref,
            holder_label=holder_label,
            currency=currency,
            period_start=start,
            period_end=end,
            opening_balance=opening,
            closing_balance=closing,
            transactions=transactions,
            anomalies=anomalies,
        )


@dataclass
class _RawRow:
    booked: date
    value: date
    description: str
    amount: Decimal             # unsigned movement as printed
    balance: Decimal            # running balance printed on the row
    column_signed: Decimal      # sign from the DEBITO / CREDITO column position
    balance_signed: Decimal | None  # sign from the balance change, if magnitudes agree
    row_no: int
