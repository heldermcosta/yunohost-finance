"""Parser for "Cartão Affinity" (BBVA Consumer Finance / Affinity-Inditex
Mastercard) credit card statements.

Layout notes (see the project doc for the full write-up):
  - Every statement has one or two "tables": the old layout (2019 - mid
    2020ish) splits INDITEX-network and non-INDITEX purchases into two
    separate tables, each with its own opening balance and Total; the newer
    layout (2020+) merges everything into one "MASTERCARD - Detalhe dos
    movimentos" table. Both shapes are just "a Saldo Inicial line, some
    transaction rows, a Total line" - so the parser finds tables by that
    structure (opening-balance line ... Total line) rather than by the
    section header text.
  - Table boundaries are anchored on the opening-balance line ("Saldo
    Inicial" / "Saldo em dívida à data do extrato anterior") and the closing
    "Total" line, NOT on the section header text ("Operações na/fora da rede
    INDITEX:" / "<X> - Detalhe dos movimentos de ... a ..."). A handful of
    statements (several from 2020) render some static template words -
    including, in a few cases, the literal word "Total" itself - through a
    broken/garbled subset font, while the transaction data (dates,
    descriptions, amounts) and the opening-balance line stay readable.
    Rather than special-case every garbling pattern seen, this parser:
      - never relies on section-header text at all (see above);
      - falls back to the disclaimer sentence that immediately follows every
        table ("Todas as compras efe(c)tuadas com o cartão...", always
        readable in every sample seen) as the table's closing signal when no
        literal "Total" line was found - the last no-date charge line seen
        is then the (garbled) Total, not a real charge;
      - treats a line with no trailing "X,XX €" amount as harmless noise
        (column-header wraparound, page footer/legal boilerplate, the
        payment-info block below the table) regardless of what it says, and
        only flags a line as a genuine anomaly if it *does* end in an amount
        but didn't match any of the known row shapes.
  - A few statements also have no-date charge lines for interest/stamp duty
    ("Juros", "Imposto Selo Art. 17.2.1/17.3.1 TGIS") right before Total -
    these are real charges and are kept as transactions, dated to the most
    recent preceding transaction date in that table.
  - Every statement's own "Pagamento por DD/Multibanco/transferencia" line
    (the cardholder's monthly card payment) is deliberately dropped, not
    imported as a transaction: that cash movement already appears as a debit
    in the ActivoBank current-account statement itself, so keeping it here
    would double-count it in spending/income totals. Because the shared
    reconciliation check (opening + net movement == closing) needs to hold
    for the transactions actually kept, the reported opening balance is
    adjusted by the amount of the dropped payment(s) - see
    _section_reconciliation_opening() below. The statement's real,
    as-printed opening balance is still fully recoverable from the indexed
    PDF text (full-text search covers every statement regardless of what
    was parsed into transactions).
  - Sign convention: a credit card's "amount owed" balance moves the
    opposite way from a bank account's - a purchase (positive amount here)
    *increases* the balance, a refund/reward/cashback (negative amount here)
    *decreases* it. debit/credit are assigned by the natural
    spending-analysis meaning (debit = money spent, credit = money back)
    rather than by which one increases the card balance, so the returned
    dict sets 'balance_direction': 'debit_increases' to tell the shared
    import_file() reconciliation check to use
        opening + total_debit - total_credit == closing
    instead of the bank-account formula. See import_all.py.
"""
import re

AMT_RE = re.compile(r'-?\d{1,3}(?:\.\d{3})*,\d{2}')
OPENING_RE = re.compile(r'Saldo Inicial|Saldo em d[ií]vida [aà] data do extrato anterior', re.IGNORECASE)
TOTAL_RE = re.compile(r'^Total\b')
DISCLAIMER_RE = re.compile(r'^Todas as compras', re.IGNORECASE)
BARE_AMOUNT_RE = re.compile(r'^(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*€\s*$')
TX_RE = re.compile(r'^(\d{2}-\d{2}-\d{4})\s+(\S+)\s+(.+?)\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*€\s*$')
NODATE_RE = re.compile(r'^(.+?)\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*€\s*$')
PAYMENT_RE = re.compile(r'^pagamento por\b', re.IGNORECASE)
PERIOD_RE = re.compile(r'Detalhe dos movimentos de (\d{2}-\d{2}-\d{4}) a (\d{2}-\d{2}-\d{4})')


def clean_amount_pt(s):
    """Portuguese number format: '.' thousands separator, ',' decimal."""
    return float(s.replace('.', '').replace(',', '.'))


def to_iso(ddmmyyyy):
    dd, mm, yyyy = ddmmyyyy.split('-')
    return f"{yyyy}-{mm}-{dd}"


def _find_table_sections(text):
    """Split the statement text into one or more Opening..Total table spans
    (see module docstring). Returns a list of dicts:
        {opening: float or None, total: float or None,
         rows: [{date: 'DD-MM-YYYY' or None, description: str, amount: float}],
         anomalies: [str]}
    """
    sections = []
    cur = None
    in_table = False

    for raw in text.split('\n'):
        stripped = raw.strip()
        if not stripped:
            continue

        if OPENING_RE.search(stripped):
            cur = {'opening': None, 'total': None, 'rows': [], 'anomalies': []}
            sections.append(cur)
            in_table = True
            ms = AMT_RE.findall(stripped)
            if ms:
                cur['opening'] = clean_amount_pt(ms[-1])
            continue

        if not in_table or cur is None:
            continue

        if TOTAL_RE.match(stripped):
            ms = AMT_RE.findall(stripped)
            if ms:
                cur['total'] = clean_amount_pt(ms[-1])
            in_table = False
            continue

        if DISCLAIMER_RE.match(stripped):
            if cur['total'] is None and cur['rows'] and cur['rows'][-1]['date'] is None:
                # The literal "Total" text was garbled/blanked in this
                # statement; the last no-date line collected is the closing
                # total, not a real charge.
                cur['total'] = cur['rows'].pop()['amount']
            in_table = False
            continue

        mtx = TX_RE.match(stripped)
        if mtx:
            date, _cardtok, desc, amt = mtx.groups()
            cur['rows'].append({'date': date, 'description': desc.strip(), 'amount': clean_amount_pt(amt)})
            continue

        mnd = NODATE_RE.match(stripped)
        if mnd:
            desc, amt = mnd.groups()
            cur['rows'].append({'date': None, 'description': desc.strip(), 'amount': clean_amount_pt(amt)})
            continue

        mba = BARE_AMOUNT_RE.match(stripped)
        if mba:
            cur['rows'].append({'date': None, 'description': '', 'amount': clean_amount_pt(mba.group(1))})
            continue

        # See module docstring: only a line that DOES end in an amount but
        # matched none of the row shapes above is a genuine anomaly.
        if '€' in stripped and AMT_RE.search(stripped):
            cur['anomalies'].append(f"unrecognized line inside table: {stripped!r}")

    return sections


def parse_affinity(text, filename):
    sections = _find_table_sections(text)
    if not sections:
        return None  # not a statement with a transaction table (e.g. an annual fee notice)

    all_opening_known = all(s['opening'] is not None for s in sections)
    all_total_known = all(s['total'] is not None for s in sections)

    anomalies = []
    for s in sections:
        anomalies.extend(s['anomalies'])
        if s['opening'] is None or s['total'] is None:
            anomalies.append("section missing opening balance or Total - statement left unreconciled")

    transactions = []
    adjusted_opening_total = 0.0 if all_opening_known else None
    true_closing_total = 0.0 if all_total_known else None

    for s in sections:
        payment_sum_this_section = 0.0
        last_date_iso = None
        for r in s['rows']:
            if r['date']:
                last_date_iso = to_iso(r['date'])
            is_payment = bool(PAYMENT_RE.match(r['description']))
            if is_payment:
                payment_sum_this_section += r['amount']
                continue  # deliberately not imported - see module docstring
            lanc_date = to_iso(r['date']) if r['date'] else last_date_iso
            if lanc_date is None:
                anomalies.append(f"charge with no date and no preceding dated row to fall back to: {r!r}")
                continue
            amt = r['amount']
            if amt >= 0:
                debit, credit = amt, None
            else:
                debit, credit = None, -amt
            transactions.append({
                'lanc_date': lanc_date, 'valor_date': None, 'description': r['description'],
                'debit': debit, 'credit': credit, 'balance': None,
            })
        if s['opening'] is not None and adjusted_opening_total is not None:
            adjusted_opening_total += s['opening'] + payment_sum_this_section
        if s['total'] is not None and true_closing_total is not None:
            true_closing_total += s['total']

    dated = sorted(t['lanc_date'] for t in transactions)
    period_start = dated[0] if dated else None
    period_end = dated[-1] if dated else None
    if not dated:
        # A statement with only a payment has no dated charge: take the period from its heading.
        period = PERIOD_RE.search(text)
        if period:
            period_start, period_end = to_iso(period.group(1)), to_iso(period.group(2))

    return {
        'period_start': period_start,
        'period_end': period_end,
        'account_name': 'Cartão Affinity',
        'account_number': None,
        'currency': 'EUR',
        'saldo_inicial': adjusted_opening_total,
        'saldo_final': true_closing_total,
        'balance_direction': 'debit_increases',
        'transactions': transactions,
        'anomalies': anomalies,
    }
