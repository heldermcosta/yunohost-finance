"""Parser for "Cartão Cofidis" credit card statements.

Layout notes (see the project doc for the full write-up):
  - Single transaction table per statement ("MOVIMENTOS NO PERÍODO DO
    EXTRATO"), anchored between the "Descritivo" column-header word (on the
    second of two header lines) and the "DETALHES DO MONTANTE A PAGAR"
    section that always follows it - the only reliable end-of-table
    anchor, because...
  - ...a statement with enough transactions runs onto a second (or third)
    PDF page, and the page break drops a full page-footer's worth of
    boilerplate (a horizontal dot-rule, "Caso discorde de algum dos
    movimentos...", the legal paragraph, the repeated column header, the
    "Cartão nº ... Nome Titular ..." footer line) *into the middle of the
    table* before more transaction rows resume. None of that boilerplate
    may be treated as "end of table" (`BOILERPLATE_RE`/`DOTLINE_RE`) - it
    only ends the *row currently being built* (so it never gets glued onto
    a description), while `in_table` stays true so the rows after the
    page break are still collected.
  - A row's description can wrap onto a second physical line (a merchant
    address fragment, a store code, or - critically - the "(-)" credit
    marker itself, which for some entries lands on the wrapped line
    instead of the dated line). Continuation lines are appended to the row
    being built rather than treated as new rows or as noise.
  - Column layout is irregular row to row (a domestic PT purchase has
    Montante Original/Moeda/Tx.câmbio/Montante(EUR)/CashBack all present; a
    "(-)" credit-style row only ever prints Montante Original=0,00 then
    Montante(EUR) directly, with Moeda/Tx.câmbio blank), so rather than a
    fixed-column regex, the row is split into date_op/date_val/rest, and
    the rest's *last two* PT-formatted numbers are always Montante(EUR)
    and CashBack in that order, regardless of how many optional fields
    come before them. CashBack is a per-row informational accrual only -
    it's paid out later via an explicit "CashBack (-)" row when redeemed,
    so it is not itself a balance movement and is not imported.
  - Sign convention: a row is a credit (reduces the balance owed) if "(-)"
    appears anywhere in its (possibly two-line) description - this covers
    "Pagamento por Multibanco/débito direto (-)", "Devolução de Compras
    (-)", "CashBack (-)", and "Anulação CRED. ... (-)" (a previously-posted
    credit being reversed - confirmed against the RESUMO's own Créditos
    Diversos total, which includes it). Everything else is a debit
    (COMP./Pag. Serv./cash-advance-style transfers). balance_direction is
    set to 'debit_increases' as for every other card - see
    parse_affinity.py's docstring for the general reasoning.
  - "Anulação COMP. ..." rows (a voided *purchase* authorization that never
    actually posted) are the one exception: dropped entirely, counted as
    neither debit nor credit. Confirmed against the RESUMO's Compras and
    Créditos Diversos totals, neither of which includes them - and every
    sample seen renders the *exact same* "Anulação COMP." line 3 times in
    a row (a rendering artifact, not 3 real events).
  - Every statement's own "Pagamento por Multibanco/débito direto (-)"
    line(s) (the cardholder's own card payment) are dropped for the same
    reason as every other card parser (see parse_affinity.py) - already a
    debit in the ActivoBank statement - with the reported opening balance
    adjusted down by the dropped amount(s) so reconciliation still holds
    for the transactions actually kept.
  - The RESUMO block ("RESUMO DOS MOVIMENTOS NO PERÍODO DO EXTRATO") gives
    the statement's own opening/closing balance directly (first/last of
    its 9 "X,XX€" tokens: anterior, Compras, Cash advance, Pagamento
    Serviços, Encargos, Débitos Diversos, Pagamentos, Créditos Diversos,
    atual). The 5th token (Encargos) is also used to catch a real gap in
    the movements table: statements up to ~2022 charge a monthly card
    membership fee ("Mais por 1€" - 1,00€, occasionally 1,01€/2,00€) that
    is folded into Encargos but never itemized as its own row anywhere in
    the movements table (2024+ statements DO itemize it, as "Comissão
    Serviços Adicionais" + "Imposto Selo sobre Comissão Serviços
    Adicionais" rows). Whenever Encargos exceeds whatever fee-like rows
    *are* itemized, the shortfall is added as one synthetic transaction
    (dated to period_end) rather than left as a silent, permanent
    reconciliation gap.
"""
import re

AMT_RE = re.compile(r'-?\d{1,3}(?:\.\d{3})*,\d{2}')
TRAILING_AMT_RE = re.compile(r'-?\d{1,3}(?:\.\d{3})*,\d{2}\s*€?\s*$')
PERIOD_RE = re.compile(r'Per[ií]odo de extrato de\s+(\d{2}/\d{2}/\d{4})\s+a\s+(\d{2}/\d{2}/\d{4})')
ACCOUNT_RE = re.compile(r'Cart[aã]o n[ºo°]\s*(\S+)', re.IGNORECASE)
HEADER_ANCHOR_RE = re.compile(r'Descritivo', re.IGNORECASE)
RESUMO_ANCHOR_RE = re.compile(r'RESUMO DOS MOVIMENTOS NO PER[IÍ]ODO DO EXTRATO', re.IGNORECASE)
RESUMO_ROW_RE = re.compile(r'^(?:-?\d{1,3}(?:\.\d{3})*,\d{2}€\s*){8,}$')
DOTLINE_RE = re.compile(r'^\.{10,}$')
END_TABLE_RE = re.compile(r'DETALHES DO MONTANTE A PAGAR', re.IGNORECASE)
# Page-break boilerplate that can land mid-table - see module docstring.
BOILERPLATE_RE = re.compile(
    r'Caso discorde de algum|Este documento n[aã]o serve de fatura|'
    r'Cofidis \(sucursal|NIPC/CRC Lisboa|Institui[cç][aã]o de cr[eé]dito registada|'
    r'N[aã]o Vida, registada|liberdade de estabelecimento|Pens[oõ]es sob o|'
    r'Cart[aã]o n[ºo°]\s*XX|Edif[ií]cio Natura Towers|'
    r'^Data\s+Data|^Opera[cç][aã]o\s+valor\s+Descritivo',
    re.IGNORECASE
)
TX_RE = re.compile(r'^(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(.+)$')
PAYMENT_RE = re.compile(r'^pagamento por\b', re.IGNORECASE)
ANULACAO_COMP_RE = re.compile(r'^anula[cç][aã]o\s+comp\.', re.IGNORECASE)
FEE_KEYWORDS_RE = re.compile(r'Comiss[aã]o|Imposto do Selo|Imposto Selo|^Juros\b|^Seguro\b|\bISCC\b', re.IGNORECASE)


def clean_amount_pt(s):
    """Portuguese number format: '.' thousands separator, ',' decimal."""
    return float(s.replace('.', '').replace(',', '.'))


def to_iso(ddmmyyyy):
    dd, mm, yyyy = ddmmyyyy.split('/')
    return f"{yyyy}-{mm}-{dd}"


def parse_cofidis(text, filename):
    pm = PERIOD_RE.search(text)
    if not pm:
        return None  # not a statement (e.g. a Cofinet/onboarding PDF)
    period_start = to_iso(pm.group(1))
    period_end = to_iso(pm.group(2))

    acc = ACCOUNT_RE.search(text)
    account_number = acc.group(1) if acc else None

    saldo_inicial = saldo_final = None
    resumo_encargos = None
    rm = RESUMO_ANCHOR_RE.search(text)
    if rm:
        after = text[rm.end():]
        for raw in after.split('\n')[:15]:
            stripped = raw.strip()
            if RESUMO_ROW_RE.match(stripped):
                nums = AMT_RE.findall(stripped)
                saldo_inicial = clean_amount_pt(nums[0])
                saldo_final = clean_amount_pt(nums[-1])
                if len(nums) == 9:
                    resumo_encargos = clean_amount_pt(nums[4])
                break

    rows = []
    unmatched_anomalies = []
    in_table = False
    saw_header_anchor = False
    current = None  # row being built; description can wrap onto a 2nd line

    def flush():
        if current is not None:
            rows.append(current)

    for raw in text.split('\n'):
        stripped = raw.strip()
        if not stripped:
            continue

        if not in_table and HEADER_ANCHOR_RE.search(stripped):
            saw_header_anchor = True
            in_table = True
            continue

        if not in_table:
            continue

        if END_TABLE_RE.search(stripped):
            flush()
            current = None
            in_table = False
            continue

        if DOTLINE_RE.match(stripped) or BOILERPLATE_RE.search(stripped):
            # Page-break noise: ends the row being built, not the table.
            flush()
            current = None
            continue

        mtx = TX_RE.match(stripped)
        if mtx:
            flush()
            d1, _d2, rest = mtx.groups()
            nums = AMT_RE.findall(rest)
            if len(nums) < 2:
                current = None
                unmatched_anomalies.append(f"unrecognized line inside table: {stripped!r}")
                continue
            montante_eur = clean_amount_pt(nums[-2])
            # description: text before the first 2+space gap that precedes
            # either a 2-letter uppercase country code or a digit run.
            dm = re.split(r'\s{2,}(?=[A-Z]{2}\s|\d)', rest, maxsplit=1)
            desc = dm[0].strip()
            current = {'date': d1, 'description': desc, 'amount': montante_eur}
            continue

        if current is not None:
            current['description'] = (current['description'] + ' ' + stripped).strip()
            continue

        if TRAILING_AMT_RE.search(stripped):
            unmatched_anomalies.append(f"unrecognized line inside table: {stripped!r}")

    flush()

    if not saw_header_anchor:
        return None  # not a statement with a transaction table

    anomalies = list(unmatched_anomalies)
    if saldo_inicial is None or saldo_final is None:
        anomalies.append("could not find opening/closing balance in RESUMO block - statement left unreconciled")

    # "Anulação COMP." rows never posted to the balance at all - see
    # module docstring - dropped before any of the totals below.
    rows = [r for r in rows if not ANULACAO_COMP_RE.search(r['description'])]

    transactions = []
    payment_sum = 0.0
    itemized_fees = 0.0
    for r in rows:
        is_credit = '(-)' in r['description']
        is_payment = is_credit and bool(PAYMENT_RE.match(r['description']))
        if is_payment:
            payment_sum += r['amount']
            continue  # deliberately not imported - see module docstring
        if not is_credit and FEE_KEYWORDS_RE.search(r['description']):
            itemized_fees += r['amount']
        lanc_date = to_iso(r['date'])
        debit = None if is_credit else r['amount']
        credit = r['amount'] if is_credit else None
        transactions.append({
            'lanc_date': lanc_date, 'valor_date': None, 'description': r['description'],
            'debit': debit, 'credit': credit, 'balance': None,
        })

    if resumo_encargos is not None:
        fee_shortfall = round(resumo_encargos - itemized_fees, 2)
        if fee_shortfall > 0.005:
            transactions.append({
                'lanc_date': period_end, 'valor_date': None,
                'description': 'Comissão Serviço Adicional (encargo mensal não detalhado no extrato)',
                'debit': fee_shortfall, 'credit': None, 'balance': None,
            })

    adjusted_opening = (saldo_inicial - payment_sum) if saldo_inicial is not None else None

    return {
        'period_start': period_start,
        'period_end': period_end,
        'account_name': 'Cartão Cofidis',
        'account_number': account_number,
        'currency': 'EUR',
        'saldo_inicial': adjusted_opening,
        'saldo_final': saldo_final,
        'balance_direction': 'debit_increases',
        'transactions': transactions,
        'anomalies': anomalies,
    }
