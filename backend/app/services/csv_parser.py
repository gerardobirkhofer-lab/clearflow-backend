import csv
import io
from datetime import datetime
from typing import List, Dict, Optional

def detect_bank_format(headers):
    headers_lower = [h.lower().strip() for h in headers]
    header_str = " ".join(headers_lower)
    if "santander" in header_str:
        return "santander"
    elif "bbva" in header_str:
        return "bbva"
    elif "caixa" in header_str:
        return "caixabank"
    elif "sabadell" in header_str:
        return "sabadell"
    return "generic_spanish"

def parse_european_amount(value):
    if not value:
        return 0.0
    value = value.replace("€", "").replace("EUR", "").strip()
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        parts = value.split(",")
        if len(parts[-1]) == 2 and len(parts) == 2:
            value = value.replace(",", ".")
        else:
            value = value.replace(",", "")
    try:
        return float(value)
    except ValueError:
        return 0.0

def parse_spanish_date(value):
    formats = ["%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%Y-%m-%d", "%d.%m.%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None

def parse_bank_csv(file_content, filename=""):
    first_lines = "\n".join(file_content.split("\n")[:5])
    delimiter = ";" if ";" in first_lines else ","
    reader = csv.DictReader(io.StringIO(file_content), delimiter=delimiter)
    headers = reader.fieldnames or []
    bank_format = detect_bank_format(headers)
    transactions = []
    
    for row in reader:
        if not any(row.values()):
            continue
        date_val = None
        for key in ["Fecha", "Data", "fecha", "data", "Fecha operación", "Fecha valor"]:
            if key in row and row[key]:
                date_val = row[key]
                break
        concept = ""
        for key in ["Concepto", "Concepte", "Descripción", "Descripcion", "concepto", "concepte"]:
            if key in row and row[key]:
                concept = row[key]
                break
        amount = 0.0
        for key in ["Importe", "Import", "importe", "import", "Cantidad"]:
            if key in row and row[key]:
                amount = parse_european_amount(row[key])
                break
        balance = None
        for key in ["Saldo", "saldo", "SALDO"]:
            if key in row and row[key]:
                balance = parse_european_amount(row[key])
                break
        reference = ""
        for key in ["Referencia", "Ref.", "referencia", "Nº operación"]:
            if key in row and row[key]:
                reference = row[key]
                break
        parsed_date = parse_spanish_date(date_val) if date_val else None
        
        if parsed_date and concept:
            transactions.append({
                "transaction_date": parsed_date,
                "concept": concept,
                "amount": amount,
                "balance": balance,
                "reference": reference,
                "bank_name": bank_format,
                "raw_data": str(row),
            })
    return transactions
