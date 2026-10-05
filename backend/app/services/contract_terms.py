"""Read a fee and a payout wait from a contract, then apply the confirmed rule."""
from __future__ import annotations

import io
import re
import unicodedata
from datetime import date, datetime, timedelta

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
WEEKDAY_WORDS = {
    "lunes": "monday",
    "martes": "tuesday",
    "miercoles": "wednesday",
    "jueves": "thursday",
    "viernes": "friday",
    "sabado": "saturday",
    "domingo": "sunday",
}


def fold(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text or "")
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return stripped.lower()


def _number(raw: str) -> float:
    return float(raw.replace(",", "."))


def propose_terms(text: str) -> dict:
    """Pick the commission and the wait that the wording points to. Leave a blank when it is unclear."""
    folded = fold(text)
    fee_percent = _percent(folded)
    fee_fixed = _fixed(folded)
    payout_days = _days(folded)
    close_weekday = _weekday(folded)
    if payout_days is None and close_weekday:
        extra = re.search(r"(?:mas|\+)\s*(\d{1,2})\s*dias", folded)
        if extra:
            payout_days = int(extra.group(1))
    return {
        "fee_percent": fee_percent,
        "fee_fixed": fee_fixed,
        "payout_days": payout_days,
        "close_weekday": close_weekday,
        "found": any(value is not None for value in (fee_percent, fee_fixed, payout_days, close_weekday)),
    }


def text_from_file(filename: str, content: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".txt"):
        return content.decode("utf-8-sig", errors="replace")
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError("only pdf or txt")


def expected_net(amount: float, fee_percent: float, fee_fixed: float) -> float:
    gross = float(amount or 0)
    return round(gross - gross * float(fee_percent or 0) / 100.0 - float(fee_fixed or 0), 2)


def expected_arrival(sale_on, payout_days, close_weekday) -> date | None:
    if sale_on is None or payout_days is None:
        return None
    start = sale_on.date() if isinstance(sale_on, datetime) else sale_on
    if close_weekday in WEEKDAYS:
        delta = (WEEKDAYS.index(close_weekday) - start.weekday()) % 7
        start = start + timedelta(days=delta)
    return start + timedelta(days=int(payout_days))


def _percent(text: str) -> float | None:
    hits = []
    for match in re.finditer(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%", text):
        window = text[max(0, match.start() - 80):match.end()]
        score = 0
        for word in ("comision", "tarifa", "fee", "descuento", "mdr", "tpv"):
            if word in window:
                score += 1
        hits.append((score, _number(match.group(1))))
    if not hits:
        return None
    best = max(score for score, _value in hits)
    if best == 0 and len(hits) != 1:
        return None
    for score, value in hits:
        if score == best and 0 <= value <= 100:
            return value
    return None


def _fixed(text: str) -> float | None:
    for sentence in re.split(r"[.\n]", text):
        if not any(word in sentence for word in ("fijo", "por operacion", "por transaccion")):
            continue
        match = re.search(r"(\d{1,3}(?:[.,]\d{2}))\s*(?:€|euros?)", sentence)
        if not match:
            match = re.search(r"(?:€|eur)\s*(\d{1,3}(?:[.,]\d{2}))", sentence)
        if match:
            value = _number(match.group(1))
            if 0 <= value < 10000:
                return value
    return None


def _days(text: str) -> int | None:
    pattern = r"(?:d\s*\+\s*|a los\s+|abono en\s+|plazo de\s+|llega\s+)(\d{1,2})\s*dias"
    hits = []
    for match in re.finditer(pattern, text):
        window = text[max(0, match.start() - 60):match.end() + 20]
        score = 0
        for word in ("abono", "liquidacion", "ingreso", "pago"):
            if word in window:
                score += 1
        day_count = int(match.group(1))
        if 0 <= day_count <= 60:
            hits.append((score, day_count))
    if not hits:
        return None
    best = max(score for score, _value in hits)
    for score, value in hits:
        if score == best:
            return value
    return None


def _weekday(text: str) -> str | None:
    match = re.search(
        r"cierre[^.]{0,50}(" + "|".join(WEEKDAY_WORDS) + ")",
        text,
    )
    if not match:
        return None
    return WEEKDAY_WORDS[match.group(1)]
