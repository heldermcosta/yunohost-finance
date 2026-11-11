"""Parser for "Cartão WiZink" credit card statements.

Layout notes (see the project doc for the full write-up):
  - Unlike Affinity, WiZink statements have exactly one transaction table per
    statement, anchored between the column-header line ("Descrição da
    transação") and one of a few "end of table" marker lines ("Pagamentos
    efetuados no período...", "Total de pagamentos efetuados",
    "P Pagamentos efetuados..."). The header-anchor text appears mid-line
    (after the "Data        Data" column headers), so it must be found with
    a search, not an anchored match.
  - Two balance-line wordings have been seen across the statement history:
    the original "Saldo em dívida à data do extrato anterior/atual <amount>
    €" (through ~2023-07), and "...anterior/atual (Total) <amount> €" from
    ~2023-08 onward. Both are matched by one regex with an optional
    "(Total)" chunk. Label and value must be kept on the *same physical
    line* ([ \\t]* between them, never \\s*, which matches newlines too) -
    the naive \\s* version silently matched across a table-header row that
    repeats the same label text with no value on that line, grabbing an
    unrelated number (the credit limit) from the row below instead.
  - A few rows per statement's life render with blank date columns in the
    text layer while description and amount stay intact (seen on a handful
    of "P Pagamento por multibanco" rows), and a few render with EVERYTHING
    blank except the amount - a small interest/fee-looking charge that
    always appeared right after the header row, before the first dated row,
    in every sample seen. Both are still captured (date optional, then a
    bare-amount-only fallback) and given a date via fallback: the next
    dated row's date if there is one (matching every sample seen), else the
    previous dated row's date, else the statement's period-start date.
  - Every statement's own "P Pagamento por ..." line(s) (the cardholder's
    monthly card payment, by direct debit or Multibanco) are deliberately
    dropped, not imported as a transaction - see parse_affinity.py's
    docstring for why (the same reasoning applies here verbatim: that cash
    movement already appears as a debit in the ActivoBank statement). The
    reported opening balance is adjusted by the sum of the dropped
    payment(s) so the shared reconciliation check still holds exactly for
    the transactions actually kept.
  - Sign convention and 'balance_direction': same as Affinity - a purchase
    (positive amount) increases the balance owed, a refund/credit (negative
    amount) decreases it, so this parser sets
    'balance_direction': 'debit_increases' (see import_all.py).
  - Three statement files in this folder
    (..._20220215.pdf, ..._20220315.pdf, ..._20220417.pdf) are confirmed
    corrupted on disk (87-byte HTML error stubs, not real PDFs) - like the
    0-byte ActivoBank file, this is a pre-existing, unfixable data gap, not
    something this parser can work around. A handful of other files in the
    folder (a service-agreement PDF, a NIB-change notice, an unrelated
    "missões" doc, a signed contract) are not statements at all and are
    correctly skipped (no transaction table found).
"""
import re

AMT_RE = re.compile(r'-?\d{1,3}(?:\.\d{3})*,\d{2}')
PERIOD_RE = re.compile(r'De\s+(\d{2}/\d{2}/\d{4})\s+a\s+(\d{2}/\d{2}/\d{4})')
ACCOUNT_RE = re.compile(r'conta-cart[aã]o n[ºo°]\s*([\d ]+)', re.IGNORECASE)
# "(Total)" was inserted into the label wording starting ~2023-08; [ \t]*
# (never \s*, which spans newlines) keeps label+value pinned to one line.
PREV_BAL_RE = re.compile(r'Saldo em d[ií]vida [aà] data do extrato anterior(?:[ \t]*\(Total\))?[ \t]*([\d.,]+)[ \t]*€', re.IGNORECASE)
CUR_BAL_RE = re.compile(r'Saldo em d[ií]vida [aà] data do extrato atual(?:[ \t]*\(Total\))?[ \t]*([\d.,]+)[ \t]*€', re.IGNORECASE)
HEADER_ANCHOR_RE = re.compile(r'Descri[cç][aã]o da transa[cç][aã]o', re.IGNORECASE)
# Date prefix is optional: a handful of rows (e.g. some "P Pagamento por
# multibanco" rows) render with both date columns blank in the text layer,
# while the description and amount stay intact.
TX_RE = re.compile(r'^(?:(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+)?([A-Z]\s+)?(.+?)\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*€\s*$')
# Some rows render with EVERYTHING blank except the amount - see docstring.
BARE_AMOUNT_RE = re.compile(r'^(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*€\s*$')
END_MARKERS = ('Pagamentos efetuados no per', 'Total de pagamentos efetuados', 'P Pagamentos efetuados')
PAYMENT_RE = re.compile(r'^pagamento por\b', re.IGNORECASE)


def clean_amount_pt(s):
    """Portuguese number format: '.' thousands separator, ',' decimal."""
    return float(s.replace('.', '').replace(',', '.'))


def to_iso_slash_ddmm(ddmm, year_hint):
    """'DD/MM' plus a year taken from the nearest full 'DD/MM/YYYY' period
    date -> 'YYYY-MM-DD'. Statement periods never cross a year boundary in
    the samples seen for this card, so a single year_hint per statement is
    enough."""
    dd, mm = ddmm.split('/')
    return f"{year_hint}-{mm}-{dd}"


def parse_wizink(text, filename):
    pm = PERIOD_RE.search(text)
    if not pm:
        return None  # not a statement with a transaction period (e.g. an agreement/notice PDF)
    d1, m1, y1 = pm.group(1).split('/')
    d2, m2, y2 = pm.group(2).split('/')
    period_start_full = f"{y1}-{m1}-{d1}"
    period_end_full = f"{y2}-{m2}-{d2}"

    acc = ACCOUNT_RE.search(text)
    account_number = re.sub(r'\s+', ' ', acc.group(1)).strip() if acc else None

    pb = PREV_BAL_RE.search(text)
    cb = CUR_BAL_RE.search(text)
    saldo_inicial = clean_amount_pt(pb.group(1)) if pb else None
    saldo_final = clean_amount_pt(cb.group(1)) if cb else None

    rows = []
    unmatched_anomalies = []
    in_table = False
    saw_header_anchor = False

    for raw in text.split('\n'):
        stripped = raw.strip()
        if not stripped:
            continue

        if HEADER_ANCHOR_RE.search(stripped):
            saw_header_anchor = True
            in_table = True
            continue

        if not in_table:
            continue

        if any(stripped.startswith(m) or m in stripped for m in END_MARKERS):
            in_table = False
            continue

        mtx = TX_RE.match(stripped)
        if mtx:
            d, _d2, _marker, desc, amt = mtx.groups()
            rows.append({'date': d, 'description': desc.strip(), 'amount': clean_amount_pt(amt)})
            continue

        mba = BARE_AMOUNT_RE.match(stripped)
        if mba:
            rows.append({'date': None, 'description': '', 'amount': clean_amount_pt(mba.group(1))})
            continue

        # Only a line that DOES end in an amount but matched neither row
        # shape above is a genuine anomaly (mirrors parse_affinity.py).
        if '€' in stripped and AMT_RE.search(stripped):
            unmatched_anomalies.append(f"unrecognized line inside table: {stripped!r}")

    if not saw_header_anchor:
        return None  # not a statement with a transaction table

    # Date fallback for rows whose date columns were blank in the text
    # layer: use the next dated row's date (every blank-date row seen so
    # far precedes the first dated row), else the previous dated row's
    # date, else the statement's period-start date.
    next_date = None
    for r in reversed(rows):
        if r['date'] is not None:
            next_date = r['date']
        elif next_date is not None:
            r['date'] = next_date
    prev_date = None
    for r in rows:
        if r['date'] is not None:
            prev_date = r['date']
        elif prev_date is not None:
            r['date'] = prev_date
        else:
            r['date'] = pm.group(1)  # DD/MM/YYYY period-start, handled below

    anomalies = list(unmatched_anomalies)
    if saldo_inicial is None or saldo_final is None:
        anomalies.append("could not find opening/closing balance line - statement left unreconciled")

    transactions = []
    payment_sum = 0.0
    for r in rows:
        raw_date = r['date']
        if '/' in raw_date and raw_date.count('/') == 2:
            lanc_date = f"{raw_date.split('/')[2]}-{raw_date.split('/')[1]}-{raw_date.split('/')[0]}"
        else:
            lanc_date = to_iso_slash_ddmm(raw_date, y1)
        is_payment = bool(PAYMENT_RE.match(r['description']))
        if is_payment:
            payment_sum += r['amount']
            continue  # deliberately not imported - see module docstring
        amt = r['amount']
        if amt >= 0:
            debit, credit = amt, None
        else:
            debit, credit = None, -amt
        transactions.append({
            'lanc_date': lanc_date, 'valor_date': None, 'description': r['description'],
            'debit': debit, 'credit': credit, 'balance': None,
        })

    adjusted_opening = (saldo_inicial + payment_sum) if saldo_inicial is not None else None

    return {
        'period_start': period_start_full,
        'period_end': period_end_full,
        'account_name': 'Cartão WiZink',
        'account_number': account_number,
        'currency': 'EUR',
        'saldo_inicial': adjusted_opening,
        'saldo_final': saldo_final,
        'balance_direction': 'debit_increases',
        'transactions': transactions,
        'anomalies': anomalies,
    }


BARE_AMOUNT_TEXT_RE = re.compile(r'^(-?\d{1,3}(?:\.\d{3})*,\d{2}) €$')
AMOUNT_TOKEN_RE = re.compile(r'^-?\d{1,3}(?:\.\d{3})*,\d{2}$')
DATE_TOKEN_RE = re.compile(r'^\d{2}/\d{2}$')
OCR_ROW_RE = re.compile(r'(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+)$')  # OCR can add a stray letter before the date


def repair_bare_rows(layout_text, pdf_path, password):
    """Read the date and description of rows whose text layer holds only the amount.

    Some WiZink rows are drawn with their date and description as a bitmap, so
    `pdftotext -layout` gives just the amount on that line. The same amount is found
    in the word-position text, cropped from the page and read with OCR. Only a
    row whose OCR gives both dates and a description is changed; anything else is
    left exactly as it was. Amounts are matched in page order.
    """
    from ..pdfpos import ocr_crop, positional_lines

    # Amounts printed alone inside the transaction table, in reading order. The table
    # is found the same way parse_wizink finds it: from the header line to an end marker.
    amount_boxes = []  # (page, y_min, y_max, x_min, amount_text)
    in_table = False
    for line in positional_lines(pdf_path, password):
        if HEADER_ANCHOR_RE.search(line.text):
            in_table = True
            continue
        if any(line.text.startswith(m) or m in line.text for m in END_MARKERS):
            in_table = False
            continue
        if not in_table or any(DATE_TOKEN_RE.match(w[4]) for w in line.words):
            continue
        for k in range(len(line.words) - 1):
            x0, _x1, y0, y1, token = line.words[k]
            if line.words[k + 1][4] == '€' and AMOUNT_TOKEN_RE.match(token):
                amount_boxes.append((line.page, y0, y1, x0, token))

    out = []
    cursor = 0
    in_table = False
    for raw in layout_text.split('\n'):
        stripped = raw.strip()
        if HEADER_ANCHOR_RE.search(stripped):
            in_table = True
        elif any(stripped.startswith(m) or m in stripped for m in END_MARKERS):
            in_table = False
        bare = BARE_AMOUNT_RE.match(stripped) if in_table else None
        if bare:
            amount_text = bare.group(1)
            match_idx = next(
                (k for k in range(cursor, len(amount_boxes)) if amount_boxes[k][4] == amount_text),
                None,
            )
            if match_idx is not None:
                cursor = match_idx + 1
                page, y0, y1, x0, _token = amount_boxes[match_idx]
                ocr = ocr_crop(pdf_path, page, 0, x0 - 2, y0 - 6, y1 + 6, password)
                m = OCR_ROW_RE.search(ocr)
                if m:
                    indent = raw[:len(raw) - len(raw.lstrip())]
                    raw = f"{indent}{m.group(1)} {m.group(2)} {m.group(3).strip()} {stripped}"
        out.append(raw)
    return '\n'.join(out)
