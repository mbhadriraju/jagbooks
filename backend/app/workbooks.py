"""Rolling, template-preserving workbooks. Adapted from the root workbook prototype."""
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile
from xml.etree.ElementTree import ParseError
from zlib import error as CompressionError

from fastapi import HTTPException
from openpyxl import load_workbook
from openpyxl.workbook.properties import CalcProperties
from openpyxl.utils.exceptions import InvalidFileException

from app.models import CATEGORIES
from app.schemas import TransactionInput

TEMPLATE = Path(__file__).parent / "assets" / "finance-template.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
WORKBOOK_BUCKET = "bvw-finance-workbooks"
MAX_CENTS = 100_000_000_000
# Same classification/row layout as workbook.py. Bank deposits write the numeric
# Balance Sheet source so Cash Flow's existing linked formula stays intact.
DESTINATIONS = {}
for kind, sheet, first in [("revenue", "Income Statement", 4), ("expense", "Income Statement", 13),
                           ("asset", "Balance Sheet", 4), ("liability", "Balance Sheet", 13),
                           ("cash_flow", "Cash Flow Statement", 4), ("purchase_order", "Cash Flow Statement", 14)]:
    for offset, category in enumerate(CATEGORIES[kind]):
        row = first + offset
        if kind == "asset" and offset >= 1:
            row += 1  # Bank Deposits occupies row 5 in the source template.
        DESTINATIONS[(kind, category)] = (sheet, row)
DESTINATIONS[("cash_flow", "bank_deposits")] = ("Balance Sheet", 5)


def invalid(message):
    raise HTTPException(422, message)


def load(content):
    if not 0 < len(content) <= 10 * 1024 * 1024:
        invalid("Workbook must be between 1 byte and 10 MiB.")
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(e.file_size for e in entries) > 40 * 1024 * 1024:
                invalid("Workbook expanded size exceeds limit.")
            if any("vbaProject" in e.filename or e.filename.startswith("xl/externalLinks/") for e in entries):
                invalid("Use an XLSX without macros or external workbook links.")
        return load_workbook(BytesIO(content), data_only=False, keep_links=False)
    except HTTPException:
        raise
    except (BadZipFile, KeyError, ValueError, OSError, TypeError, IndexError, ParseError, InvalidFileException, CompressionError, EOFError, OverflowError):
        invalid("Invalid XLSX workbook.")


def month(header):
    if isinstance(header, (date, datetime)):
        return header.year, header.month
    if isinstance(header, str):
        try:
            parsed = datetime.strptime(header, "%b-%y")
            return parsed.year, parsed.month
        except ValueError:
            return None
    return None


def columns(wb):
    result = {}
    for name in ("Income Statement", "Balance Sheet", "Cash Flow Statement"):
        if name not in wb.sheetnames:
            invalid("Workbook requires Income Statement, Balance Sheet, and Cash Flow Statement tabs.")
        months = {}
        for cell in wb[name][1]:
            key = month(cell.value)
            if key:
                if key in months:
                    invalid("Workbook month headers must be unique.")
                months[key] = cell.column
        if not months:
            invalid("Workbook requires recognizable month headers.")
        result[name] = months
    return result


def cents(value):
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        invalid("Mapped financial input cells must contain numbers or be blank, not formulas/text.")
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value() or abs(amount) > MAX_CENTS:
            invalid("Financial amounts must be finite, within limits, and have at most two decimals.")
        return int(amount)
    except InvalidOperation:
        invalid("Invalid financial amount.")


def validate(content, baseline=None):
    wb = load(content)
    cols = columns(wb)
    reference = load(baseline if baseline is not None else TEMPLATE.read_bytes())
    reference_cols = columns(reference)
    if wb.sheetnames != reference.sheetnames:
        invalid("Workbook tabs/order must match the current workbook.")
    for name in reference.sheetnames:
        ws, old = wb[name], reference[name]
        if ws.max_row > 1000 or ws.max_column > 200 or ws.max_row != old.max_row or ws.max_column != old.max_column:
            invalid("Workbook dimensions must stay unchanged.")
        if cols[name] != reference_cols[name]:
            invalid("Workbook month columns must stay unchanged.")
        if [(t.name, t.ref) for t in ws.tables.values()] != [(t.name, t.ref) for t in old.tables.values()]:
            invalid("Workbook tables must stay unchanged.")
        for row in range(1, old.max_row + 1):
            if ws.cell(row, 1).value != old.cell(row, 1).value:
                invalid("Workbook row labels must stay unchanged.")
        for cell in old[1]:
            new = ws.cell(1, cell.column).value
            if month(cell.value) is None and new != cell.value:
                invalid("Workbook headers must stay unchanged.")
        old_formulas = {c.coordinate for row in old for c in row if c.data_type == "f"}
        new_formulas = {c.coordinate for row in ws for c in row if c.data_type == "f"}
        if old_formulas != new_formulas:
            invalid("Keep formulas in their existing cells; edit numeric inputs instead.")
    for sheet, row in set(DESTINATIONS.values()):
        for col in cols[sheet].values():
            cents(wb[sheet].cell(row, col).value)
    return wb


def summary(content):
    wb = load(content)
    return operating_totals(wb, columns(wb))


def operating_totals(wb, cols):
    result = defaultdict(lambda: {"revenue_cents": 0, "expense_cents": 0})
    for (kind, category), (sheet, row) in DESTINATIONS.items():
        if kind not in ("revenue", "expense"):
            continue
        for (year, mon), col in cols[sheet].items():
            result[f"{year:04}-{mon:02}"][kind + "_cents"] += cents(wb[sheet].cell(row, col).value)
    return {"basis": "current_workbook_inputs", "monthly": [{"month": key, **value, "net_cents": value["revenue_cents"] - value["expense_cents"]} for key, value in sorted(result.items())]}


def batch_changes(wb, rows):
    cols = columns(wb)
    totals = defaultdict(int)
    for index, data in enumerate(rows, 1):
        try:
            row = TransactionInput.model_validate({**data, "review_status": "approved"})
        except ValueError:
            invalid(f"Transaction {index} has missing/invalid fields. Correct source or workbook manually; no workbook changes applied.")
        sheet, target = DESTINATIONS[(row.financial_class, row.category)]
        col = cols[sheet].get((row.date.year, row.date.month))
        if col is None:
            invalid(f"Transaction {index} falls outside workbook month columns.")
        totals[(sheet, target, col)] += row.amount_cents
    changes = []
    for (sheet, row, col), delta in sorted(totals.items()):
        cell = wb[sheet].cell(row, col)
        before = cents(cell.value)
        value = before + delta
        if abs(value) > MAX_CENTS:
            invalid("Updated financial input exceeds amount limit.")
        if delta:
            changes.append({
                "sheet": sheet, "cell": cell.coordinate, "category": wb[sheet].cell(row, 1).value,
                "month": str(wb[sheet].cell(1, col).value), "before_cents": before,
                "delta_cents": delta, "after_cents": value,
            })
    return changes


def apply_batch(content, rows):
    wb = validate(content, content)
    for change in batch_changes(wb, rows):
        wb[change["sheet"]][change["cell"]].value = Decimal(change["after_cents"]) / 100
    wb.calculation = wb.calculation or CalcProperties()
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True
    wb.calculation.calcMode = "auto"
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def dashboard(content):
    wb = load(content)
    cash = wb["Cash Flow Statement"]
    balance = wb["Balance Sheet"]
    cols = columns(wb)
    months = operating_totals(wb, cols)["monthly"]
    computed = {name: {} for name in wb.sheetnames}
    income = wb["Income Statement"]
    template = load(TEMPLATE.read_bytes())
    for item in months:
        key = tuple(map(int, item["month"].split("-")))
        income_col = cols[income.title][key]
        for row, metric in ((10, "revenue_cents"), (23, "expense_cents"), (25, "net_cents")):
            cell = income.cell(row, income_col)
            related = (10, 23, 25) if row == 25 else (row,)
            if all(income.cell(r, income_col).value == template[income.title].cell(r, income_col).value
                   for r in related):
                computed[income.title][cell.coordinate] = item[metric]
        col = cols[cash.title][key]
        letter = cash.cell(11, col).column_letter
        header = cash.cell(1, col).value
        item["cash_flow_cents"] = None
        if (cash.cell(11, col).value == f"=SUM({letter}4:{letter}10)"
                and cash.cell(5, col).value == f"=Table3[[#This Row],[{header}]]"):
            try:
                item["cash_flow_cents"] = (
                    sum(cents(cash.cell(row, col).value) for row in (4, 6, 7, 8, 9, 10))
                    + cents(balance.cell(5, cols[balance.title][key]).value)
                )
                computed[cash.title][cash.cell(11, col).coordinate] = item["cash_flow_cents"]
            except HTTPException:
                pass  # A custom input/formula may require Excel to calculate.
    return {
        "monthly": months,
        "tabs": [{"name": ws.title, "computed": computed[ws.title], "rows": [
            [cell.value if cell.value is None or isinstance(cell.value, (bool, int, float))
             else str(cell.value) for cell in row] for row in ws.rows
        ]} for ws in wb],
    }
