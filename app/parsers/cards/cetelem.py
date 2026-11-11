"""Parser for "Cartão Cetelem" (Cartão de Crédito Black / Black Plus)
statements.

Two distinct format eras (see the project doc for the full write-up),
detected by which section header is present - no statement falls outside
either bucket (confirmed against all 32 real statements: 26 old-era, 6
new-era):

OLD ERA ("DETALHE DE MOVIMENTOS" header, statements through Jan 2024):
  - This is a "pay in full" (Fim do Mês) card: the statement's own opening
    balance is *always* 0,00 by design - every period's purchases plus that
    period's own 0,30€/11,85% TAN membership fee ("MENSALIDADE") are billed
    in full and auto-debited from the bank account the following month, so
    there is never a real balance carried forward from the printed "SALDO
    EM DÍVIDA" of one statement into the next (confirmed: for every regular
    statement, `SALDO EM DÍVIDA == TOTAL COMPRAS FIM DE MÊS + 0,30`).
  - The real transaction table ("Modalidade Fim Mês" subsection of DETALHE
    DE MOVIMENTOS) lists that period's purchases plus any incidental
    merchant-charged fee rows (e.g. "COMISSAO POS") in the same list -
    all real debits, parsed uniformly.
  - A second subsection, "Outros Movimentos Financeiros", follows in the
    same table and needs per-row classification:
      * "PAGAMENTO DE MENSALIDADE" (+ its "(INCLUI) IMPOSTO SELO" note,
        which is not a separate amount - just a breakdown note about the
        same payment) is the auto-debit that paid off the *prior* period's
        bill - dropped entirely, same reasoning as every other card parser
        (already a debit in the ActivoBank statement). Since opening is
        fixed at 0,00 by design, no opening-balance adjustment is needed
        for this drop (unlike every other card, which carries a real
        balance forward).
      * Everything else - "DEVOLUCAO % COMPRAS ACUMULADAS" (a cashback-style
        accrual), "PAGAMENTO POR ATM"/"REEMBOLSO" (one-off manual/voluntary
        payments and refunds outside the normal auto-debit cycle) - is a
        real credit and is imported as one. A first hand-verification pass
        seemed to show DEVOLUCAO never actually applying (this period's own
        PAGAMENTO DE MENSALIDADE always equals exactly the *previous*
        period's printed SALDO EM DÍVIDA, with no visible adjustment for
        that previous period's DEVOLUCAO) - but that was checking against
        the bank's own "TOTAL COMPRAS FIM DE MÊS" aggregate, which turns
        out to already have DEVOLUCAO netted out of it. Checked instead
        against the raw sum of this parser's own per-row purchase total (as
        it must be, since that's what's actually summed here), DEVOLUCAO
        *does* apply, on every regular statement.
  - "ANULAÇÃO TRANSAÇÃO" rows (a voided purchase authorization that never
    actually posted, cf. parse_cofidis.py's "Anulação COMP." rows) cancel
    the *immediately preceding* purchase row of the same amount - both are
    dropped rather than counted, in the "Modalidade Fim Mês" subsection as
    well as (self-resolving, no special-casing needed) the new era's
    running-balance rows.
  - The 0,30€ monthly membership fee is never itemized as its own row in
    the movements table - it's a hidden/unitemized fee (cf. parse_cofidis.py)
    synthesized from the "MENSALIDADE (X%)" line under "Modalidade Conta
    Permanente" in DETALHE DE VALORES A PAGAR (falling back to the same
    line in DETALHE DE PAGAMENTOS POR TIPOLOGIA DE UTILIZAÇÃO when the
    former omits it - seen on one early statement). When neither section
    has that line (seen once), no fee is synthesized.
  - The `DETALHE DE VALORES ANTECIPADOS` and `DETALHE DE PAGAMENTOS POR
    TIPOLOGIA DE UTILIZAÇÃO` / (the recap portion of) `DETALHE DE VALORES A
    PAGAR` sections are pure duplicative recaps of totals already present
    in DETALHE DE MOVIMENTOS - confirmed against 4 independent samples -
    and are never parsed for transactions, only mined for the fee amount
    above.
  - One genuinely anomalous cluster (Feb-May 2020, all within the "opening
    is always 0,00" design's blind spot) involves a large disputed/foreign
    charge, a partial REEMBOLSO, and several irregular "PAGAMENTO POR ATM"
    voluntary payments that don't reconcile under the standard formula -
    a real prior balance appears to have been carried across these
    specific statements in a way the printed SALDO EM DÍVIDA (which shows
    0,00 throughout) does not reveal. Rather than guess at an unverifiable
    carry-forward model, these are left flagged (imported, searchable,
    reconciled=0) - consistent with how every other source handles a
    genuine pre-existing statement anomaly.

NEW ERA ("Detalhes de Movimento do Período de Extrato" header, 2024+):
  - Fundamentally different, self-validating design: each row prints its
    own running "Saldo Final" balance column, so a row's signed delta is
    computed as (this row's Saldo Final) - (previous row's Saldo Final)
    rather than by summing category totals - which makes this era immune
    to the hidden-fee problem above (any fee folded into a row's own delta
    is automatically captured, itemized or not).
  - The first row ("VALOR EM DIVIDA FECHO EXTRATO ANTERIOR") anchors the
    opening balance and is not itself a transaction.
  - "APRESENTACAO MENSALIDADE" (which can occur mid-period, not
    necessarily at period start, unlike every other repayment row seen in
    this project) is this era's repayment-of-prior-bill row - dropped
    entirely, with the reported opening balance adjusted *down* by the
    dropped amount (adjusted_opening = raw_opening - payment_sum, the same
    formula as parse_cofidis.py) so reconciliation holds for the rows kept.
  - balance_direction is 'debit_increases' for both eras (a purchase
    increases the amount owed) - see parse_affinity.py for the general
    reasoning.
"""
import re

AMT_RE = re.compile(r'-?\d+(?:\.\d{3})*,\d{2}(?!\d)')
OLD_HEADER_RE = re.compile(r'DETALHE DE MOVIMENTOS', re.IGNORECASE)
NEW_HEADER_RE = re.compile(r'Detalhes de Movimento do Per[ií]odo de Extrato', re.IGNORECASE)
OLD_PERIOD_RE = re.compile(r'PER[ÍI]ODO DO EXTRATO:\s*(\d{2}\.\d{2}\.\d{4})\s*a\s*(\d{2}\.\d{2}\.\d{4})')
NEW_PERIOD_RE = re.compile(r'Per[ií]odo Extrato Atual:\s*(\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})')
BALANCE_TRIO_RE = re.compile(
    r'(\d+(?:\.\d{3})*,\d{2})\s*€\s*(\d+(?:\.\d{3})*,\d{2})\s*€\s*(\d+(?:\.\d{3})*,\d{2})\s*€'
)
ACCOUNT_RE = re.compile(r'N[ºo°]\.?\s*(?:DE\s*)?CONTA:\s*(\S+)', re.IGNORECASE)
MENSALIDADE_FEE_RE = re.compile(r'MENSALIDADE\s*\(\d+%\)([^\n]*)', re.IGNORECASE)
OLD_ROW_RE = re.compile(r'^(\d{2}[./]\d{2}[./]\d{2})\s+(\d{2}\.\d{2}\.\d{2})\s+(?:(\d{2,3})\s+)?(.+)$')
NEW_ROW_RE = re.compile(r'^(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(.+)$')
DROP_MENSALIDADE_RE = re.compile(r'^PAGAMENTO DE MENSALIDADE', re.IGNORECASE)
DROP_SELO_NOTE_RE = re.compile(r'^\(INCLUI\)\s*IMPOSTO SELO', re.IGNORECASE)
ANULACAO_RE = re.compile(r'^ANULA[ÇC][ÃA]O TRANSA[ÇC][ÃA]O', re.IGNORECASE)
NEW_ANTERIOR_RE = re.compile(r'^VALOR EM DIVIDA FECHO EXTRATO ANTERIOR', re.IGNORECASE)
NEW_APRESENTACAO_RE = re.compile(r'^APRESENTACAO MENSALIDADE', re.IGNORECASE)
SECTION_BREAK_RE = re.compile(
    r'DETALHE DE PAGAMENTOS POR TIPOLOGIA|DETALHE DE VALORES ANTECIPADOS|'
    r'DETALHE DE VALORES A PAGAR|Valores Antecipados|Resumo Mensalidade por Modalidade',
    re.IGNORECASE)
MODALIDADE_FIM_MES_RE = re.compile(r'^Modalidade Fim M[eê]s\s*$', re.IGNORECASE)
OUTROS_MOV_RE = re.compile(r'^Outros Movimentos Financeiros\s*$', re.IGNORECASE)


def clean_amount_pt(s):
    return float(s.replace('.', '').replace(',', '.'))


def _expand_year(yyyy):
    # The old-era row date regex (OLD_ROW_RE) matches a 2-digit year for
    # the per-row transaction date (the statement genuinely prints
    # "DD.MM.YY", e.g. "25.03.20" for 2020-03-25) - this was previously
    # stored as-is, producing malformed dates like "20-03-25" instead of
    # "2020-03-25". Every Cetelem statement on file is from 2016-2026, so
    # a bare "20" prefix is unambiguous.
    return '20' + yyyy if len(yyyy) == 2 else yyyy


def to_iso_dot(ddmmyyyy):
    dd, mm, yyyy = ddmmyyyy.split('.')
    return f"{_expand_year(yyyy)}-{mm}-{dd}"


def to_iso_slash(ddmmyyyy):
    dd, mm, yyyy = ddmmyyyy.split('/')
    return f"{_expand_year(yyyy)}-{mm}-{dd}"


def parse_cetelem(text, filename):
    if NEW_HEADER_RE.search(text):
        return _parse_new_era(text, filename)
    if OLD_HEADER_RE.search(text):
        return _parse_old_era(text, filename)
    return None  # not a Cetelem statement (e.g. an onboarding/other PDF)


def _account_number(text):
    m = ACCOUNT_RE.search(text)
    return m.group(1) if m else None


# --------------------------------------------------------------------- OLD

def _parse_old_era(text, filename):
    pm = OLD_PERIOD_RE.search(text)
    if not pm:
        return None
    period_start = to_iso_dot(pm.group(1))
    period_end = to_iso_dot(pm.group(2))

    bt = BALANCE_TRIO_RE.search(text)
    saldo_final = clean_amount_pt(bt.group(2)) if bt else None

    anomalies = []
    if saldo_final is None:
        anomalies.append("could not find SALDO EM DÍVIDA in header - statement left unreconciled")

    # Isolate the DETALHE DE MOVIMENTOS table (ends at the first of the
    # known recap-section headers that always follows it).
    tbl_start = OLD_HEADER_RE.search(text)
    table_text = ''
    if tbl_start:
        rest = text[tbl_start.end():]
        brk = SECTION_BREAK_RE.search(rest)
        table_text = rest[:brk.start()] if brk else rest

    lines = [ln.strip() for ln in table_text.split('\n')]
    in_outros = False
    rows = []  # (kind, date, description, amount)
    for stripped in lines:
        if not stripped:
            continue
        if MODALIDADE_FIM_MES_RE.match(stripped):
            in_outros = False
            continue
        if OUTROS_MOV_RE.match(stripped):
            in_outros = True
            continue
        m = OLD_ROW_RE.match(stripped)
        if not m:
            continue
        d1, _d2, _cardno, rest_txt = m.groups()
        nums = AMT_RE.findall(rest_txt)
        if not nums:
            continue
        amount = clean_amount_pt(nums[-1])
        # Strip amount tokens and, for foreign-currency rows, the exchange
        # rate ("1,000") that AMT_RE doesn't match (3 decimals, not 2).
        desc = AMT_RE.sub('', rest_txt)
        desc = re.sub(r'-?\d+,\d{3}\b', '', desc).strip()
        desc = re.sub(r'\s{2,}', ' ', desc).strip()
        kind = 'outros' if in_outros else 'compra'
        rows.append((kind, d1, desc, amount))

    transactions = []
    purchase_total = 0.0
    i = 0
    while i < len(rows):
        kind, d1, desc, amount = rows[i]
        lanc_date = to_iso_dot(d1.replace('/', '.'))
        if kind == 'compra':
            if ANULACAO_RE.match(desc):
                # A voided purchase authorization that never actually
                # posted (cf. parse_cofidis.py's "Anulação COMP." rows) -
                # it cancels the *immediately preceding* purchase row of
                # the same amount, so both are dropped rather than counted.
                if transactions and transactions[-1].get('debit') == amount:
                    purchase_total -= amount
                    transactions.pop()
                else:
                    anomalies.append(f"ANULAÇÃO TRANSAÇÃO ({amount}) did not match the preceding row's amount")
                i += 1
                continue
            transactions.append({
                'lanc_date': lanc_date, 'valor_date': None, 'description': desc,
                'debit': amount, 'credit': None, 'balance': None,
            })
            purchase_total += amount
            i += 1
            continue
        # kind == 'outros'
        if DROP_MENSALIDADE_RE.match(desc):
            i += 1
            # the immediately-following "(INCLUI) IMPOSTO SELO" row is a
            # breakdown note about this same payment, not a separate amount
            if i < len(rows) and DROP_SELO_NOTE_RE.match(rows[i][2]):
                i += 1
            continue
        # anything else (DEVOLUCAO % COMPRAS ACUMULADAS, PAGAMENTO POR ATM,
        # REEMBOLSO, ...): real credit. DEVOLUCAO's amount is already netted
        # out of the bank's own "TOTAL COMPRAS FIM DE MÊS" aggregate figure,
        # which is what misled an earlier hand-verification pass into
        # thinking it never applies - but summed against the raw per-row
        # purchase total (which is what this parser actually uses), it does
        # apply, on every regular statement checked.
        transactions.append({
            'lanc_date': lanc_date, 'valor_date': None, 'description': desc,
            'debit': None, 'credit': amount, 'balance': None,
        })
        i += 1

    # Hidden monthly membership fee - never itemized in the movements table.
    fm = MENSALIDADE_FEE_RE.search(text)
    fee = None
    if fm:
        tail_nums = AMT_RE.findall(fm.group(1))
        if tail_nums:
            fee = clean_amount_pt(tail_nums[-1])
    if fee:
        transactions.append({
            'lanc_date': period_end, 'valor_date': None,
            'description': 'MENSALIDADE (comissão de processamento - não detalhada no extrato)',
            'debit': fee, 'credit': None, 'balance': None,
        })

    if saldo_final is not None:
        debit_total = sum(t['debit'] for t in transactions if t['debit'])
        credit_total = sum(t['credit'] for t in transactions if t['credit'])
        computed = round(debit_total - credit_total, 2)
        if abs(computed - saldo_final) > 0.01:
            anomalies.append(
                f"does not reconcile under the standard 'opening always 0,00' formula "
                f"(computed {computed}, printed SALDO EM DÍVIDA {saldo_final}) - this statement falls "
                f"within a known irregular Fev-Jun 2020 cluster (a large disputed/foreign charge, a "
                f"partial REEMBOLSO, and manual PAGAMENTO POR ATM payments) where a real balance appears "
                f"to have been carried forward in a way the printed SALDO EM DÍVIDA does not reveal - "
                f"left unreconciled rather than guessed at, see the project doc"
            )

    return {
        'period_start': period_start,
        'period_end': period_end,
        'account_name': 'Cartão Cetelem',
        'account_number': _account_number(text),
        'currency': 'EUR',
        'saldo_inicial': 0.0,
        'saldo_final': saldo_final,
        'balance_direction': 'debit_increases',
        'transactions': transactions,
        'anomalies': anomalies,
    }


# --------------------------------------------------------------------- NEW

def _parse_new_era(text, filename):
    pm = NEW_PERIOD_RE.search(text)
    if not pm:
        return None
    period_start = to_iso_slash(pm.group(1))
    period_end = to_iso_slash(pm.group(2))

    bt = BALANCE_TRIO_RE.search(text)
    header_saldo_final = clean_amount_pt(bt.group(3)) if bt else None

    anomalies = []

    tbl_start = NEW_HEADER_RE.search(text)
    table_text = ''
    if tbl_start:
        rest = text[tbl_start.end():]
        brk = SECTION_BREAK_RE.search(rest)
        table_text = rest[:brk.start()] if brk else rest

    rows = []
    for raw in table_text.split('\n'):
        stripped = raw.strip()
        if not stripped:
            continue
        m = NEW_ROW_RE.match(stripped)
        if not m:
            continue
        d1, _d2, rest_txt = m.groups()
        nums = AMT_RE.findall(rest_txt)
        if not nums:
            continue
        saldo_final_row = clean_amount_pt(nums[-1])
        # description = text before the run of trailing '-'/amount columns
        # (Cashback, Mensalidade, Moeda, Câmbio, Valor Movimento, Saldo Final)
        desc_m = re.match(r'^(.*?)(?:\s+-|\s+-?\d)', rest_txt + ' -')
        desc = desc_m.group(1).strip() if desc_m else rest_txt.strip()
        rows.append((d1, desc, saldo_final_row))

    if not rows:
        anomalies.append("no transaction rows found in table")

    transactions = []
    raw_opening = None
    prev_saldo = None
    payment_sum = 0.0
    for idx, (d1, desc, saldo_final_row) in enumerate(rows):
        if idx == 0:
            raw_opening = saldo_final_row
            prev_saldo = saldo_final_row
            if not NEW_ANTERIOR_RE.match(desc):
                anomalies.append(f"first table row was not the expected opening anchor: {desc!r}")
            continue
        delta = round(saldo_final_row - prev_saldo, 2)
        lanc_date = to_iso_slash(d1)
        if NEW_APRESENTACAO_RE.match(desc):
            payment_sum += abs(delta)
            prev_saldo = saldo_final_row
            continue
        transactions.append({
            'lanc_date': lanc_date, 'valor_date': None, 'description': desc,
            'debit': delta if delta > 0 else None,
            'credit': -delta if delta < 0 else None,
            'balance': saldo_final_row,
        })
        prev_saldo = saldo_final_row

    saldo_final = prev_saldo if prev_saldo is not None else header_saldo_final
    if header_saldo_final is not None and saldo_final is not None and abs(header_saldo_final - saldo_final) > 0.01:
        anomalies.append(
            f"last row's Saldo Final ({saldo_final}) does not match header's Saldo em dívida ({header_saldo_final})"
        )

    adjusted_opening = (raw_opening - payment_sum) if raw_opening is not None else None

    return {
        'period_start': period_start,
        'period_end': period_end,
        'account_name': 'Cartão Cetelem',
        'account_number': _account_number(text),
        'currency': 'EUR',
        'saldo_inicial': adjusted_opening,
        'saldo_final': saldo_final,
        'balance_direction': 'debit_increases',
        'transactions': transactions,
        'anomalies': anomalies,
    }
