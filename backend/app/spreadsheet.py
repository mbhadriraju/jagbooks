"""Generate portable XLSX from an immutable snapshot without local disk state."""
from collections import defaultdict
from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def worksheet(rows):
    result = [f'<worksheet xmlns="{NS}"><sheetData>']
    for index, row in enumerate(rows, 1):
        result.append(f'<row r="{index}">')
        for value in row:
            if isinstance(value, (int, Decimal)):
                result.append(f'<c><v>{value}</v></c>')
            else:
                # Inline strings never interpreted as formulas, even if starting with '='.
                text = "".join(c for c in str(value or "") if ord(c) >= 32 or c in "\t\n\r")
                result.append(f'<c t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>')
        result.append('</row>')
    return ''.join(result) + '</sheetData></worksheet>'


def workbook(snapshot):
    ledger = [["Date", "Reference", "Description", "Amount (USD)", "Financial class", "Category", "Source file ID"]]
    totals = defaultdict(int)
    for row in snapshot:
        ledger.append([row["date"], row.get("reference"), row["description"], Decimal(row["amount_cents"]) / 100, row["financial_class"], row["category"], row.get("source_file_id")])
        totals[(row["date"][:7], row["financial_class"], row["category"])] += row["amount_cents"]
    monthly = [["Month", "Financial class", "Category", "Amount (USD)"]]
    monthly.extend([*key, Decimal(value) / 100] for key, value in sorted(totals.items()))
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Transactions" sheetId="1" r:id="rId1"/><sheet name="Monthly totals" sheetId="2" r:id="rId2"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', worksheet(ledger))
        archive.writestr('xl/worksheets/sheet2.xml', worksheet(monthly))
    return output.getvalue()
