"""Parser for "Cartão Universo" credit card statements (Sonae Financial
Services / Banco BNP Paribas Personal Finance, later Universo IME).

Layout notes (see the project doc for the full write-up):
  - Real transactions live between an explicit "Saldo em dívida (à data do
    extrato anterior)" / "(à data de fecho do extrato anterior)" opening
    marker and the matching "...atual)" closing marker - these two phrases
    (BLOCK_START_RE / BLOCK_END_RE) are the only anchors trusted, because...
  - ...at least one very early statement (230795870_201603.pdf) embeds a
    full fixed instructional annex ("PASSO A PASSO, CONHEÇA O SEU EXTRATO")
    that reproduces a FAKE replica statement page, complete with its own
    "Conta de Pagamento a Crédito:"/"Saldo em dívida (anterior/atual)"
    markers and fabricated transaction rows dated a year earlier than the
    real statement. Rather than try to parse around it, the whole document
    is truncated at the first "PASSO A PASSO" occurrence before any anchor
    search runs - everything after it is discarded unseen. (This annex was
    only ever observed on that one 2016 statement; the truncation is a
    no-op, and therefore harmless, on every other statement.)
  - A statement can be long enough to span multiple PDF pages, and a page
    break can reprint the section header and "Saldo em dívida (anterior)"
    line *mid-table* with the same anterior amount before continuing the
    same transaction list (seen on 230795870_201812.pdf). This is detected
    (a new "anterior" marker whose amount exactly matches the currently
    open block's own anterior) and treated as a no-op continuation rather
    than a second block - a genuine second block always reports a
    *different* anterior (it's the previous block's own closing balance).
  - Column layout is irregular row to row: a domestic purchase prints
    [Comissões e Despesas, Valor Total, Valor do desconto, % de Desconto];
    a foreign-currency purchase additionally prints Valor Moeda and Valor
    Euros before those four; and some repayment rows print only 3 fields.
    Rather than a fixed-column regex, each row's amount tokens are
    extracted in order and "Valor Total" is picked out by position -
    3rd-from-end for every purchase/credit row (see extract_valor_total),
    which holds regardless of how many optional fields precede it and
    regardless of whether the trailing "% de Desconto" rate actually
    rendered with its '%' character (some eras print it as a bare number).
  - Repayment-type rows (see REPAYMENT_RE: "PAGAMENTO UNIVERSO", "PAGAMENTO
    DE MENSALIDADE", "DEBITO BANCO", "REEMBOLSO ANTECIPADO" - the various
    ways this card's own balance gets auto-debited/repaid from the bank
    account) use a *different* column layout with no desconto/cashback
    pair at all, so the position heuristic above doesn't apply to them;
    their amount is instead taken as the largest-magnitude non-percentage
    token on the row, which matches every sample seen. These rows are
    dropped from the transactions table entirely - for the same
    double-counting reason as every other card parser (the repayment
    already appears as a debit in the ActivoBank statement) - with the
    block's own opening balance adjusted *up* by the dropped amount so
    reconciliation still holds. (Note the sign: unlike every other card
    parser here, Universo prints "Saldo em dívida" using bank-account sign
    conventions - a purchase is a credit-reducing... no, a purchase makes
    it *more negative* and a repayment less negative/positive - so this
    parser uses the plain bank-account formula (opening + credit - debit =
    closing), not the 'debit_increases' flip every other card uses.)
  - Dates in the table are DD/MM only (no year, except in some 2016-era
    statements which do print a 2-digit year) - the year is taken from the
    statement's own period end, except a December date on a statement
    whose period spans a year boundary, which takes the period start's
    year instead.
"""
import re

AMT_TOK_RE = re.compile(r'(-?\d{1,3}(?:\.\d{3})*,\d{2})(%)?')
PERIOD_RE = re.compile(r'Movimentos de:\s*(\d{2}/\d{2}/\d{4})\s+a\s+(\d{2}/\d{2}/\d{4})')
PASSO_RE = re.compile(r'PASSO A PASSO', re.IGNORECASE)
BLOCK_START_RE = re.compile(r'Saldo em d[ií]vida\s*\(à data (?:do extrato|de fecho do extrato) anterior\)')
BLOCK_END_RE = re.compile(r'=?\s*Saldo em d[ií]vida\s*\(à data (?:do extrato|de fecho do extrato) atual\)')
ROW_RE = re.compile(r'^(\d{2}/\d{2}(?:/\d{2,4})?)\s+(\d{2}/\d{2}(?:/\d{2,4})?)\s+(?:(\d{2,3})\s+)?(.*)$')
PAGE_NOISE_RE = re.compile(
    r'^(Data\s+Data|Realiza[cç][aã]o Valor|Cart[aã]o\s+Movimentos|Comiss[oõ]es|Descontos Cart[aã]o|'
    r'\* Inclui Imposto|Movimentos de:|EXTRATO DE MOVIMENTOS|CONTA DE CR[ÉE]DITO|www\.universo\.pt|'
    r'Apoio ao Cliente|2ª a Dom\.|Atendimento 24h|Despesas\*|desconto Desconto|Moeda\s+C[aâ]mbio|'
    r'Valor do\s+% de|Valor\s+Total|e\s+Total|Galp\s+Desconto|% de\s+Compras|Comb\.|Euros)',
    re.IGNORECASE)
SECTION_RE = re.compile(r'Conta de Pagamento a (Cr[ée]dito|D[ée]bito):')
SUBTOTAL_RE = re.compile(
    r'Subtotal (total movimentos cart[aã]o|movimentos na conta)|'
    r'^\+\s*(Movimentos Modalidade|Outros Movimentos)|'
    r'Modalidade Fim do M[eê]s \+ Outros Encargos',
    re.IGNORECASE)
REPAYMENT_RE = re.compile(
    r'^(PAGAMENTO UNIVERSO|PAGAMENTO DE MENSALIDADE|DEBITO BANCO|REEMBOLSO ANTECIPADO)',
    re.IGNORECASE)
# Page-break legal/footer boilerplate that can land mid-table (company name,
# registration numbers, capital social, address) - see module docstring.
BOILERPLATE_RE = re.compile(
    r'Capital Social|matriculada na CRC|NIPC[:/]|Banco BNP Paribas|Universo, IME|Sonae Financial|'
    r'Entidade Concessora|Institui[cç][aã]o de Moeda Eletr[oó]nica|SFS\s*-\s*Financial|'
    r'Inscrita no Banco de Portugal|Sede: Lugar do Espido',
    re.IGNORECASE)
NOISE_CONTINUATION_RE = re.compile(
    r'^(al Finance|Sede:|Universo,|Sonae|Banco BNP|www\.|Apoio ao|2ª a Dom|Atendimento|matriculada)',
    re.IGNORECASE)


def clean_amount_pt(s):
    """Portuguese number format: '.' thousands separator, ',' decimal."""
    return float(s.replace('.', '').replace(',', '.'))


def extract_valor_total(nums):
    """nums: list of (value_str, has_pct) tuples in row order. Valor Total
    is always 3rd-from-end (see module docstring), except a bare 3-token
    row (no desconto/rate pair at all) where it's 2nd-from-end."""
    n = len(nums)
    if n == 0:
        return None
    idx = 1 if n == 3 else max(0, n - 3)
    return clean_amount_pt(nums[idx][0])


def to_iso4(ddmmyyyy):
    dd, mm, yyyy = ddmmyyyy.split('/')
    return f"{yyyy}-{mm}-{dd}"


def parse_universo(text, filename):
    passo = PASSO_RE.search(text)
    if passo:
        text = text[:passo.start()]

    pm = PERIOD_RE.search(text)
    if not pm:
        return None  # not a statement (e.g. the onboarding contract PDF)
    period_start_raw, period_end_raw = pm.group(1), pm.group(2)
    period_start = to_iso4(period_start_raw)
    period_end = to_iso4(period_end_raw)
    period_end_yyyy = period_end_raw.split('/')[2]

    lines = text.split('\n')
    n = len(lines)

    blocks = []  # [{'anterior': float, 'atual': float, 'rows': [...]}]
    current_block = None
    current_row = None
    anomalies = []
    saw_any_block = False
    # The last line with no amounts seen outside a row. On some statements the first
    # line of a wrapped description is printed just above its row's date line.
    pending_text = None

    def flush_row():
        nonlocal current_row
        if current_row is not None and current_block is not None:
            current_block['rows'].append(current_row)
        current_row = None

    def amount_after(s2_end_idx, after_text):
        m = AMT_TOK_RE.search(after_text)
        if m:
            return m.group(1)
        for j in range(s2_end_idx, min(s2_end_idx + 3, n)):
            s2 = lines[j].strip()
            if s2:
                m2 = AMT_TOK_RE.search(s2)
                return m2.group(1) if m2 else None
        return None

    i = 0
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        i += 1
        if not stripped:
            continue

        mstart = BLOCK_START_RE.search(stripped)
        if mstart:
            flush_row()
            saw_any_block = True
            amt_str = amount_after(i, stripped[mstart.end():])
            new_anterior = clean_amount_pt(amt_str) if amt_str else None
            if (current_block is not None and current_block['atual'] is None
                    and new_anterior is not None and new_anterior == current_block['anterior']):
                # page-break repeat of the currently open block - see docstring
                continue
            current_block = {'anterior': new_anterior, 'atual': None, 'rows': []}
            blocks.append(current_block)
            continue

        mend = BLOCK_END_RE.search(stripped)
        if mend:
            flush_row()
            amt_str = amount_after(i, stripped[mend.end():])
            if current_block is not None:
                current_block['atual'] = clean_amount_pt(amt_str) if amt_str else None
            current_block = None
            continue

        if current_block is None:
            continue  # outside any real block (annex already truncated) - ignore

        if SUBTOTAL_RE.search(stripped) or SECTION_RE.search(stripped) or PAGE_NOISE_RE.search(stripped):
            flush_row()
            continue

        if BOILERPLATE_RE.search(stripped):
            flush_row()
            continue

        mrow = ROW_RE.match(stripped)
        if mrow:
            flush_row()
            d1, _d2, _cardnum, rest = mrow.groups()
            nums = AMT_TOK_RE.findall(rest)
            if not nums:
                current_row = {'date': d1, 'description': rest.strip()}
                pending_text = None
                continue
            desc = AMT_TOK_RE.sub('', rest).strip()
            desc = re.sub(r'\s*€\s*', ' ', desc).strip()
            desc = re.sub(r'\s{2,}', ' ', desc)
            prefixed = False
            if not desc and pending_text:
                desc = pending_text
                prefixed = True
            pending_text = None
            current_row = {'date': d1, 'description': desc, 'nums': nums, 'prefixed': prefixed}
            continue

        if not AMT_TOK_RE.search(stripped) and not NOISE_CONTINUATION_RE.search(stripped):
            pending_text = stripped

        if current_row is not None:
            # A row is "complete" once its amounts include the trailing
            # rate-flagged token - don't keep extending it with numbers
            # that belong to unrelated boilerplate/subtotal lines.
            row_complete = 'nums' in current_row and any(pct == '%' for _v, pct in current_row['nums'])
            nums_here = AMT_TOK_RE.findall(stripped) if not row_complete else []
            if nums_here and 'nums' not in current_row:
                current_row['nums'] = nums_here
                extra_desc = AMT_TOK_RE.sub('', stripped).strip()
                extra_desc = re.sub(r'\s*€\s*', ' ', extra_desc).strip()
                if extra_desc:
                    current_row['description'] += ' ' + extra_desc
            elif nums_here and 'nums' in current_row:
                current_row['nums'].extend(nums_here)
            elif (row_complete and current_row.get('prefixed') and len(stripped.split()) <= 3
                    and not re.search(r'\d', stripped)):
                # The last words of a description printed after its amounts, e.g. "DO MÊS".
                current_row['description'] = (current_row['description'] + ' ' + stripped).strip()
                current_row['prefixed'] = False
            elif not row_complete and not NOISE_CONTINUATION_RE.search(stripped):
                current_row['description'] = (current_row['description'] + ' ' + stripped).strip()
            continue

    flush_row()

    if not saw_any_block:
        return None

    transactions = []
    payment_sum = 0.0
    for block in blocks:
        if block['anterior'] is None or block['atual'] is None:
            anomalies.append(f"block missing anterior/atual balance (rows={len(block['rows'])})")
        for r in block['rows']:
            if 'nums' not in r or not r['nums']:
                anomalies.append(f"row with no amounts: {r['date']} {r['description']!r}")
                continue
            desc = r['description'].strip()
            if REPAYMENT_RE.match(desc):
                non_pct = [clean_amount_pt(v) for v, pct in r['nums'] if pct != '%']
                if not non_pct:
                    anomalies.append(f"repayment row with no amount: {r['date']} {desc!r}")
                    continue
                payment_sum += max(non_pct, key=abs)
                continue
            valor_total = extract_valor_total(r['nums'])
            dd, mm = r['date'].split('/')[:2]
            year = period_end_yyyy
            if mm == '12' and period_end_raw.split('/')[1] != '12':
                year = period_start_raw.split('/')[2]
            iso_date = f"{year}-{mm}-{dd}"
            debit = None if valor_total >= 0 else -valor_total
            credit = valor_total if valor_total >= 0 else None
            transactions.append({
                'lanc_date': iso_date, 'valor_date': None, 'description': desc,
                'debit': debit, 'credit': credit, 'balance': None,
            })

    saldo_inicial = blocks[0]['anterior'] if blocks else None
    saldo_final = blocks[-1]['atual'] if blocks else None
    adjusted_opening = (saldo_inicial + payment_sum) if saldo_inicial is not None else None

    return {
        'period_start': period_start,
        'period_end': period_end,
        'account_name': 'Cartão Universo',
        'account_number': None,
        'currency': 'EUR',
        'saldo_inicial': adjusted_opening,
        'saldo_final': saldo_final,
        'transactions': transactions,
        'anomalies': anomalies,
    }
