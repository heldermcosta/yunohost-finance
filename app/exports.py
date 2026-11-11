"""Export formats. Each function takes plain rows (already filtered by access rules) and returns bytes."""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

TRANSACTION_HEADERS = ["booked_on", "account", "description", "amount", "balance", "category", "transfer"]


def _transaction_cells(t: dict) -> list:
    return [t["booked_on"], t["account"], t["description"], t["amount"], t["balance"], t["category"],
            "yes" if t["is_transfer"] else ""]


def transactions_csv(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TRANSACTION_HEADERS)
    for t in rows:
        writer.writerow(_transaction_cells(t))
    return buffer.getvalue().encode("utf-8")


def monthly_expenses(rows: list[dict]) -> tuple[list[str], list[str], dict]:
    """Returns (months, categories, {(month, category): expenses}) for non-transfer rows."""
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for t in rows:
        if t["is_transfer"] or Decimal(t["amount"]) >= 0:
            continue
        totals[(t["booked_on"][:7], t["category"])] += -Decimal(t["amount"])
    months = sorted({m for m, _ in totals})
    categories = sorted({c for _, c in totals})
    return months, categories, totals


def transactions_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    sheet = wb.active
    sheet.title = "Transactions"
    sheet.append(TRANSACTION_HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for t in rows:
        sheet.append([
            datetime.fromisoformat(t["booked_on"]).date(), t["account"], t["description"],
            float(t["amount"]), float(t["balance"]), t["category"], "yes" if t["is_transfer"] else "",
        ])
    for row in sheet.iter_rows(min_row=2):
        row[0].number_format = "yyyy-mm-dd"
        row[3].number_format = "#,##0.00"
        row[4].number_format = "#,##0.00"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for col, width in zip("ABCDEFG", [12, 22, 48, 14, 14, 30, 10]):
        sheet.column_dimensions[col].width = width

    months, categories, totals = monthly_expenses(rows)
    summary = wb.create_sheet("Monthly expenses")
    summary.append(["Category"] + months + ["Total"])
    for cell in summary[1]:
        cell.font = Font(bold=True)
    for category in categories:
        values = [float(totals.get((m, category), 0)) for m in months]
        summary.append([category] + values + [sum(values)])
    summary.column_dimensions["A"].width = 32
    for row in summary.iter_rows(min_row=2):
        for cell in row[1:]:
            cell.number_format = "#,##0.00"

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def report_pdf(rows: list[dict], year: str, currency: str = "EUR") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    rows_in_year = [t for t in rows if t["booked_on"].startswith(year)]
    months, categories, totals = monthly_expenses(rows_in_year)
    income: dict[str, Decimal] = defaultdict(Decimal)
    for t in rows_in_year:
        if not t["is_transfer"] and Decimal(t["amount"]) > 0:
            income[t["booked_on"][:7]] += Decimal(t["amount"])

    def money(value) -> str:
        return f"{Decimal(value):,.2f}"

    styles = getSampleStyleSheet()
    story = [Paragraph(f"Spending report {year} ({currency})", styles["Title"]),
             Paragraph("Expenses by category and month. Transfers between own accounts are excluded.", styles["Normal"]),
             Spacer(1, 12)]
    header = ["Category"] + [m[5:] + "/" + m[2:4] for m in months] + ["Total"]
    body = [header]
    for category in categories:
        values = [totals.get((m, category), Decimal(0)) for m in months]
        body.append([category] + [money(v) if v else "-" for v in values] + [money(sum(values))])
    month_totals = [sum(totals.get((m, c), Decimal(0)) for c in categories) for m in months]
    body.append(["Total expenses"] + [money(v) for v in month_totals] + [money(sum(month_totals))])
    body.append(["Income"] + [money(income.get(m, 0)) for m in months] + [money(sum(income.get(m, 0) for m in months))])

    table = Table(body, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -2), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf5")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(table)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    doc.build(story)
    return buffer.getvalue()


def backup_json(payload: dict) -> bytes:
    payload = {"format": "yunohost-finance-backup", "version": 1,
               "exported_at": datetime.now(timezone.utc).isoformat(), **payload}
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
