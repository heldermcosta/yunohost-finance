"""Utility bills: electricity (EDP, Ibelectra), gas (Galp), water (Águas do Sado, SMS Setúbal,
Câmara de Palmela, EPAL) and telecoms (NOS, NOWO, Vodafone). A bill is read from its own labels. Fields that cannot be read are left empty,
never guessed. A bill with an amount but no readable street address is kept for review."""

from __future__ import annotations

import re
import subprocess
import tempfile
import unicodedata
from datetime import date
from pathlib import Path

PT_MONTHS = {"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
             "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
ABBR = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8, "set": 9,
        "out": 10, "nov": 11, "dez": 12}
NUM = r"(\d{1,3}(?:\.\d{3})*,\d{2})"
# A supply address starts with a street word; the value runs to the end of the line.
STREET = r"((?:RUA|R|AV|AVENIDA|TRAV|TRAVESSA|LARGO|ESTRADA|PRAÇA|ALAMEDA|BAIRRO|URB)\b[^\n]*)"
STREET_START = re.compile(r"^(RUA|R|AV|AVENIDA|TRAV|TRAVESSA|LARGO|ESTRADA|PRAÇA|ALAMEDA|BAIRRO|URB)\b", re.I)
# Where a supply address stops: a double space, a postcode, or the next column's label.
ADDRESS_END = (r"\s{2,}|\s\d{4}\s?-?\s?\d{3}\b|\s(RESUMO|DESIG|TAXAS|TARIF|VALOR|CUI|CONTRATO|NIF|LISBOA|"
               r"PALMELA|SETÚBAL|SETUBAL|IDENT|ATEND|Nº|N\.º|CONSUMO|DATA)\b")


def _pdf_texts(pdf_bytes: bytes, work_dir: Path) -> tuple[str, str]:
    """Raw and layout text of a PDF. Some suppliers print values in columns, so both are tried."""
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=work_dir, suffix=".pdf") as handle:
        handle.write(pdf_bytes)
        handle.flush()
        out = []
        for layout in (False, True):
            cmd = ["pdftotext"] + (["-layout"] if layout else []) + [handle.name, "-"]
            out.append(subprocess.run(cmd, capture_output=True, text=True, check=False).stdout)
    return out[0], out[1]


def money(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip().lower()
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-zç]+)\s+(?:de\s+)?(\d{4})", s)
    if m:
        month = PT_MONTHS.get(m.group(2)) or ABBR.get(m.group(2)[:3])
        if month:
            return date(int(m.group(3)), month, int(m.group(1)))
    m = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", s)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _short_date(s: str) -> date | None:
    """dd/mm/yy, as NOWO prints its billing periods."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{2})$", s.strip())
    return date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def _dotted(s: str | None) -> date | None:
    """yyyy.mm.dd, as EPAL prints its dates."""
    return parse_date(s.replace(".", "-")) if s else None


def _month_end(name: str) -> date | None:
    """The last day of a month named as "Setembro de 2022"."""
    m = re.search(r"([a-zç]+) de (\d{4})", name.lower())
    if not m or m.group(1) not in PT_MONTHS:
        return None
    year, month = int(m.group(2)), PT_MONTHS[m.group(1)]
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def _find(patterns: list[str], texts: list[str]) -> str | None:
    for pattern in patterns:
        for text in texts:
            m = re.search(pattern, text, re.I)
            if m:
                return m.group(1).strip()
    return None


def _address(value: str | None) -> str | None:
    if not value:
        return None
    value = re.split(ADDRESS_END, value.strip(), maxsplit=1)[0]
    return re.sub(r"\s+", " ", value).strip(" ,.-") or None


def _split_period(value: str | None) -> tuple[date | None, date | None]:
    if not value or not re.search(r"\sa\s", value, re.I):
        return None, None
    start, end = re.split(r"\s+a\s+", value, maxsplit=1, flags=re.I)
    return parse_date(start), parse_date(end)


def read_bill_texts(raw: str, layout: str) -> dict | None:
    """Reads one bill from its PDF text. Returns None for anything that is not a utility bill."""
    texts = [layout, raw]
    # EPAL invoices print "Débito Direto SEPA" in their payment section, so only mandate forms are dropped here.
    is_epal = bool(re.search(r"FATURA n[º°]\s*\d{12}", raw, re.I) and re.search(r"EPAL|Águas Livres", raw, re.I))
    if not is_epal and ("MANDATE" in raw.upper() or "DÉBITO DIRETO SEPA" in raw.upper()):
        return None
    bill = dict(utility=None, provider=None, address=None, invoice=None, issued=None,
                period_start=None, period_end=None, amount=None)

    if re.search(r"EDP Comercial", raw) and re.search(r"Documento emitido a|Quanto tenho", raw):
        bill.update(utility="electricity", provider="EDP")
        bill["issued"] = parse_date(_find([r"Documento emitido a:\s*(.+)"], texts))
        bill["period_start"], bill["period_end"] = _split_period(_find([r"Período de faturação:\s*(.+)"], texts))
        amount = _find([r"Quanto tenho\s+a pagar\?[\s\S]{0,300}?" + NUM + r"\s*€", r"Valor:\s*" + NUM + r"\s*€"], texts)
        bill["amount"] = money(amount) if amount else None
        bill["invoice"] = _find([r"FATURA N[º°]\s*(\d+)", r"Fatura n[º°]\s*(FT\S*\s*\S+)"], texts)
        bill["address"] = _address(_find([r"Morada de fornecimento\s*\n?\s*" + STREET], [raw, layout]))
    elif re.search(r"IBELECTRA", raw) and re.search(r"Energia e Potência|Total Faturado", raw):
        bill.update(utility="electricity", provider="Ibelectra")
        m = re.search(r"(\d{2}-\d{2}-\d{4})\s+(\d{2}-\d{2}-\d{4})", raw)
        if m:
            bill["period_start"], bill["period_end"] = parse_date(m.group(1)), parse_date(m.group(2))
        amount = _find([r"Total Faturado[\s\S]{0,300}?" + NUM + r"\s*€"], texts)
        bill["amount"] = money(amount) if amount else None
        bill["invoice"] = _find([r"\b(FT\d{6,})\b"], texts)
        bill["address"] = _address(_find([r"Morada de\s*\n?\s*Instalação\s*\n\s*" + STREET], [raw, layout]))
    elif (re.search(r"Galp", raw, re.I)
          and re.search(r"Fatura:\s*FT|DADOS DA SUA CONTA|Nº DO DOCUMENTO", raw)
          and re.search(r"valor desta fatura|Período de Faturação|Valor a pagar", raw, re.I)):
        bill.update(utility="gas", provider="Galp")
        bill["period_start"], bill["period_end"] = _split_period(_find([r"Período de Faturação:\s*(.+)"], texts))
        amount = _find([r"valor desta fatura é[\s\S]{0,40}?" + NUM, r"Valor a pagar\s*\n?\s*" + NUM], texts)
        bill["amount"] = money(amount) if amount else None
        bill["invoice"] = _find([r"Fatura:\s*(FT\s*\S+)", r"Nº DO DOCUMENTO:\s*(\S+)"], texts)
        bill["issued"] = parse_date(_find([r"Data de emiss[aã]o:?\s*(.+)", r"Data:\s*(\d{2} [A-Za-z]{3} \d{4})",
                                           r"DATA DE EMISSÃO:?\s*\n?\s*(\d{4}-\d{2}-\d{2})",
                                           r"Data de emiss[aã]o:?\s*\n?\s*(\d{4}-\d{2}-\d{2})"], texts))
        if not bill["period_end"]:
            ranges = re.findall(r"(\d{4}-\d{2}-\d{2})\s+-\s+(\d{4}-\d{2}-\d{2})", raw)
            if ranges:
                bill["period_start"] = parse_date(min(a for a, _ in ranges))
                bill["period_end"] = parse_date(max(b for _, b in ranges))
        bill["address"] = _address(_find([r"Local de fornecimento:?\s*\n?\s*" + STREET], [raw, layout]))
    elif ((re.search(r"Local de Consumo|MORADA DE FORNECIMENTO", raw, re.I)
           and re.search(r"m3|m³|Água|Serviços?", raw))
          or re.search(r"cm-palmela", raw)):
        bill.update(utility="water")
        if re.search(r"cm-palmela|Câmara Municipal de Palmela", raw, re.I):
            bill["provider"] = "Câmara de Palmela"
        elif re.search(r"Águas do Sado|aguasdosado|\bSADO\b", raw, re.I):
            bill["provider"] = "Águas do Sado"
        elif re.search(r"sms-setubal|SMS HELP", raw, re.I):
            bill["provider"] = "SMS Setúbal"
        else:
            bill["provider"] = "Água"
        bill["invoice"] = _find([r"FATURA N[º°]:?\s*(FT\s*\S+|\d\S*)", r"Fat n[º°]\s*(\d+)", r"Documento n\.?[º°]\s*(\S+)",
                                 r"FATURA n[º°]\s*(FT\s*\S+)", r"Fatura n\.º\s*(FAC\s*\S+)"], texts)
        bill["issued"] = parse_date(_find([r"Data de emiss[aã]o:?\s*(\S+)", r"DATA DE EMISS[AÃ]O\s*(\S+)"], texts))
        bill["address"] = _address(_find([r"Local de Consumo:?\s*\n?\s*" + STREET,
                                          r"MORADA DE FORNECIMENTO\s*\n\s*" + STREET], [raw, layout]))
        m = re.search(r"(\d{2}-\d{2}-\d{4})\s+a\s+(\d{2}-\d{2}-\d{4})", layout) or \
            re.search(r"(\d{2}-\d{2}-\d{4})\s+a\s+(\d{2}-\d{2}-\d{4})", raw)
        if m:
            bill["period_start"], bill["period_end"] = parse_date(m.group(1)), parse_date(m.group(2))
        amount = _find([r"Total conta cliente\s*" + NUM, r"A PAGAR AT[ÉE][^\n]*\n?[\s\S]{0,120}?" + NUM,
                        r"Total a Pagar\s*" + NUM, r"Total \(€\):\s*\n?\s*" + NUM,
                        r"Valor a pagar[\s\S]{0,60}?" + NUM], texts)
        bill["amount"] = money(amount) if amount else None
    elif re.search(r"NOS Comunica", raw) and re.search(r"Fatura n\.[º°]|Data da fatura", raw) \
            and not re.search(r"não serve de fatura", raw, re.I):
        bill.update(utility="telecom", provider="NOS")
        bill["invoice"] = _find([r"(FT\s?\d{6}/\d+)"], texts)
        bill["issued"] = parse_date(_find([r"Data da fatura[\s\S]{0,120}?(\d{2}-\d{2}-\d{4})"], texts))
        period_name = _find([r"Per[ií]odo de fatura[çc][ãa]o[\s\S]{0,60}?([A-Za-zçãéí]+ de \d{4})"], texts)
        until = _find([r"at[ée]\s+(\d{2}-\d{2}-\d{4})"], texts)
        if until:
            bill["period_end"] = parse_date(until)
        elif period_name:
            bill["period_end"] = _month_end(period_name)
        amount = _find([r"Valor desta fatura com IVA\s*€\s?" + NUM, r"Total a pagar[^\n]*€\s?" + NUM], texts)
        bill["amount"] = money(amount) if amount else None
        bill["address"] = _address(_find([r"HELDER MIGUEL ALMEIDA COSTA\s*\n[ \t]*" + STREET], [layout, raw]))
    elif re.search(r"nowo", raw, re.I) and re.search(r"FACTURA/RECIBO|Fatura Eletr", raw, re.I):
        bill.update(utility="telecom", provider="NOWO")
        bill["invoice"] = _find([r"\b([A-Z]\d{8})\b"], texts)
        bill["issued"] = parse_date(_find([r"[A-Z]\d{8}\s+(\d{2}-\d{2}-\d{4})"], texts))
        m = re.search(r"de (\d{2}/\d{2}/\d{2}) a (\d{2}/\d{2}/\d{2})", layout) or \
            re.search(r"de (\d{2}/\d{2}/\d{2}) a (\d{2}/\d{2}/\d{2})", raw)
        if m:
            bill["period_start"], bill["period_end"] = _short_date(m.group(1)), _short_date(m.group(2))
        amount = _find([r"VALOR DESTA FACTURA C/IVA\s*€\s?" + NUM, r"A PAGAR\s*€\s?" + NUM], texts)
        bill["amount"] = money(amount) if amount else None
        bill["address"] = _address(_find([r"Morada de Instala[çc][ãa]o\s*\n?\s*" + STREET], [raw, layout]))
    elif re.search(r"Vodafone Portugal|vodafone\.pt", raw) \
            and re.search(r"Valor a pagar|Montante:|Total com IVA", raw) \
            and not re.search(r"não serve de fatura|Extrato de Servi", raw, re.I):
        bill.update(utility="telecom", provider="Vodafone")
        bill["invoice"] = _find([r"Factura/Recibo N[º°]\s*(FT\s*\d{3}/\d+|\d+)", r"(FT \d{3}/\d+)"], texts)
        bill["issued"] = parse_date(_find([r"Data de Emiss[ãa]o:?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
                                           r"Data de emiss[ãa]o:?\s*(\d{1,2} [a-z]{3} \d{4})"], texts))
        m = re.search(r"Per[ií]odo de fa(?:ct|t)ura[çc][ãa]o:?\s*(\d{2}/\d{2}/\d{4}) a (\d{2}/\d{2}/\d{4})", layout) or \
            re.search(r"Per[ií]odo de fa(?:ct|t)ura[çc][ãa]o:?\s*(\d{2}/\d{2}/\d{4}) a (\d{2}/\d{2}/\d{4})", raw)
        if m:
            bill["period_start"], bill["period_end"] = parse_date(m.group(1)), parse_date(m.group(2))
        else:
            m = re.search(r"Per[ií]odo de fa(?:ct|t)ura[çc][ãa]o:?\s*(\d{1,2}) ([a-z]{3}) a (\d{1,2}) ([a-z]{3})", layout, re.I)
            if m and bill["issued"]:
                end_month = ABBR.get(m.group(4).lower())
                if end_month:
                    end = date(bill["issued"].year, end_month, int(m.group(3)))
                    if end > bill["issued"]:
                        end = end.replace(year=end.year - 1)
                    bill["period_end"] = end
        amount = _find([r"Valor a pagar\s*€\s?" + NUM, r"Montante:\s*€\s?" + NUM, r"Total com IVA\s*€\s?" + NUM], texts)
        bill["amount"] = money(amount) if amount else None
        bill["address"] = _address(_find([r"HELDER MIGUEL ALMEIDA COSTA\s*\n[ \t]*" + STREET], [layout, raw]))
    elif is_epal:
        bill.update(utility="water", provider="EPAL")
        bill["invoice"] = _find([r"FATURA n[º°]\s*(\d{12})"], texts)
        bill["issued"] = _dotted(_find([r"emitida em\s*(\d{4}\.\d{2}\.\d{2})"], texts))
        period = r"Per[ií]odo de Fa(?:ct|t)ura[çc][ãa]o[\s\S]{0,40}?(\d{4}\.\d{2}\.\d{2})\s+a\s+(\d{4}\.\d{2}\.\d{2})"
        m = re.search(period, layout, re.I) or re.search(period, raw, re.I)
        if m:
            bill["period_start"], bill["period_end"] = _dotted(m.group(1)), _dotted(m.group(2))
        amount = _find([r"Valor a Pagar\s*(\d+\.\d{2})"], texts)
        bill["amount"] = float(amount) if amount else None
        bill["address"] = _address(_find([r"Morada Postal \(Principal\)\s*-\s*(.+)"], texts))
    else:
        return None

    if not bill["amount"]:
        return None
    street = bool(bill["address"]) and bool(STREET_START.match(bill["address"]))
    bill["status"] = "ok" if street and bill["period_end"] else "review"
    return bill


def read_bill_pdf(pdf_bytes: bytes, work_dir: Path) -> dict | None:
    raw, layout = _pdf_texts(pdf_bytes, work_dir)
    return read_bill_texts(raw, layout)


def fold(text: str) -> str:
    """Upper case, no accents, single spaces: what addresses are compared by."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", plain).upper().strip()


SMALL_WORDS = {"DE", "DA", "DO", "DOS", "DAS"}


def match_key(text: str) -> str:
    """Addresses are compared without accents, punctuation and small words: "Rua de Exemplo" and
    "RUA EXEMPLO" are the same, and so are "Av. Exemplo (do) 9" and "AV EXEMPLO 9"."""
    words = re.sub(r"[^A-Z0-9]+", " ", fold(text)).split()
    return " ".join(w for w in words if w not in SMALL_WORDS)


def house_for(location: str, houses: list[tuple[str, str]]) -> str | None:
    """The house whose keyword is in the supply address. `houses` is (name, keyword) in order."""
    address = f" {match_key(location)} "
    for name, keyword in houses:
        key = match_key(keyword)
        if key and f" {key} " in address:
            return name
    return None
