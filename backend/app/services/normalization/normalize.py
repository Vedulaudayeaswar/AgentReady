"""Deterministic normalizers for prices, currencies, availability, units and identifiers.

Principles: preserve the raw value, never invent data, mark anything inferred.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from app.schemas.canonical import Measurement, ProductAttribute, ProductPrice

# Symbols that map unambiguously to one ISO code.
UNAMBIGUOUS_SYMBOLS = {
    "₹": "INR", "rs.": "INR", "rs": "INR", "inr": "INR", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
    "usd": "USD", "us$": "USD", "cad": "CAD", "c$": "CAD", "aud": "AUD", "a$": "AUD", "nzd": "NZD",
    "¥": "JPY", "jpy": "JPY", "cny": "CNY", "rmb": "CNY", "chf": "CHF", "sek": "SEK", "nok": "NOK",
    "dkk": "DKK", "aed": "AED", "sar": "SAR", "sgd": "SGD", "s$": "SGD", "hkd": "HKD", "zar": "ZAR",
    "brl": "BRL", "r$": "BRL", "mxn": "MXN", "krw": "KRW", "₩": "KRW", "pkr": "PKR", "lkr": "LKR",
    "bdt": "BDT", "৳": "BDT", "npr": "NPR", "idr": "IDR", "rp": "IDR", "myr": "MYR", "rm": "MYR",
    "php": "PHP", "₱": "PHP", "thb": "THB", "฿": "THB", "vnd": "VND", "₫": "VND", "try": "TRY", "₺": "TRY",
    "pln": "PLN", "zł": "PLN", "rub": "RUB", "₽": "RUB", "ngn": "NGN", "₦": "NGN", "kes": "KES", "egp": "EGP",
}
# Symbols shared by several currencies: we record them as inferred.
AMBIGUOUS_SYMBOLS = {"$": "USD", "kr": "SEK"}
ISO_CODES = set(UNAMBIGUOUS_SYMBOLS.values()) | {"USD", "SEK"}

_sym_pattern = "|".join(
    sorted((re.escape(k) for k in list(UNAMBIGUOUS_SYMBOLS) + list(AMBIGUOUS_SYMBOLS)), key=len, reverse=True)
)
CURRENCY_RE = re.compile(rf"(?<![A-Za-z])({_sym_pattern})(?![A-Za-z])", re.I)
NUMBER_RE = re.compile(r"\d[\d,.\s' ]*\d|\d")
PRICE_HINT_RE = re.compile(
    rf"(?:(?<![A-Za-z])(?:{_sym_pattern})\s?\d[\d,.]*|\d[\d,.]*\s?(?:{_sym_pattern})(?![A-Za-z])|\d[\d,]*\s*/-)",
    re.I,
)

AVAILABILITY_MAP = [
    (re.compile(r"\b(pre[- ]?order)\b", re.I), "PreOrder"),
    (re.compile(r"\b(back[- ]?order(ed)?)\b", re.I), "BackOrder"),
    (re.compile(r"\b(discontinued|no longer available)\b", re.I), "Discontinued"),
    (re.compile(r"\b(sold out)\b", re.I), "SoldOut"),
    (re.compile(r"\b(out of stock|unavailable|not available|currently unavailable)\b", re.I), "OutOfStock"),
    (re.compile(r"\b(only \d+ left|limited stock|few left|low stock)\b", re.I), "LimitedAvailability"),
    # Deliberately excludes bare "available" ("available in 3 colours" is not stock info).
    (re.compile(r"\b(in stock|available now|instock|ready to ship|ships today)\b", re.I), "InStock"),
]
SCHEMA_AVAILABILITY = {
    v.lower(): v
    for v in (
        "InStock", "OutOfStock", "PreOrder", "BackOrder", "Discontinued",
        "LimitedAvailability", "SoldOut", "OnlineOnly", "InStoreOnly",
    )
}

UNIT_RE = re.compile(
    r"^\s*(?P<num>[-+]?\d+(?:[.,]\d+)?(?:\s*[x×*]\s*\d+(?:[.,]\d+)?)*)\s*"
    r"(?P<unit>[A-Za-zµΩ°%\"']{1,12}(?:/[A-Za-z]{1,6})?)?\s*$"
)
KNOWN_UNITS = {
    "v": "V", "vac": "VAC", "vdc": "VDC", "a": "A", "ma": "mA", "w": "W", "kw": "kW", "hp": "HP", "rpm": "RPM",
    "hz": "Hz", "khz": "kHz", "mm": "mm", "cm": "cm", "m": "m", "in": "in", "inch": "in", "inches": "in", '"': "in",
    "ft": "ft", "kg": "kg", "g": "g", "gm": "g", "mg": "mg", "lb": "lb", "lbs": "lb", "oz": "oz", "l": "L", "ml": "mL",
    "ltr": "L", "litre": "L", "liter": "L", "mah": "mAh", "ah": "Ah", "nm": "Nm", "kgcm": "kg·cm", "°c": "°C",
    "c": "°C", "%": "%", "gb": "GB", "tb": "TB", "mb": "MB", "mp": "MP", "Ω": "Ω", "ohm": "Ω", "kpa": "kPa",
    "bar": "bar", "psi": "psi", "pcs": "pcs", "w/m": "W/m",
}


def clean_text(value: str | None, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    v = unicodedata.normalize("NFKC", str(value))
    v = re.sub(r"\s+", " ", v).strip()
    if not v:
        return None
    if max_len and len(v) > max_len:
        v = v[: max_len - 1].rstrip() + "…"
    return v


def attribute_key(label: str) -> str:
    k = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    k = re.sub(r"[^a-zA-Z0-9]+", "_", k).strip("_").lower()
    return k[:120] or "attribute"


def _parse_number(raw: str) -> Decimal | None:
    s = re.sub(r"[\s' ]", "", raw)
    if not s:
        return None
    if "," in s and "." in s:
        # whichever separator comes last is the decimal separator
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        # "1,299" -> thousands; "12,50" -> decimal; Indian "1,23,456" -> thousands
        s = s.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d.quantize(Decimal("0.01"))


def detect_currency(text: str | None) -> tuple[str | None, bool]:
    """Return (ISO code, inferred?)."""
    if not text:
        return None, False
    m = CURRENCY_RE.search(text)
    if not m:
        return None, False
    sym = m.group(1).lower()
    if sym in UNAMBIGUOUS_SYMBOLS:
        return UNAMBIGUOUS_SYMBOLS[sym], False
    return AMBIGUOUS_SYMBOLS.get(sym), True


def normalize_currency_code(code: str | None) -> str | None:
    if not code:
        return None
    c = code.strip().upper()
    if re.fullmatch(r"[A-Z]{3}", c):
        return c
    iso, _ = detect_currency(code)
    return iso


def parse_price(raw: str | float | int | Decimal | None, currency_hint: str | None = None) -> ProductPrice | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float, Decimal)):
        amount = Decimal(str(raw)).quantize(Decimal("0.01"))
        cur = normalize_currency_code(currency_hint)
        return ProductPrice(amount=amount, currency=cur, raw=str(raw))
    text = clean_text(raw) or ""
    m = NUMBER_RE.search(text)
    amount = _parse_number(m.group(0)) if m else None
    cur, inferred = detect_currency(text)
    if not cur and currency_hint:
        cur, inferred = normalize_currency_code(currency_hint), False
    if amount is None:
        return None
    return ProductPrice(amount=amount, currency=cur, raw=text[:100], currency_inferred=inferred)


def find_price_strings(text: str) -> list[str]:
    return [m.group(0) for m in PRICE_HINT_RE.finditer(text or "")]


def normalize_availability(raw: str | None) -> str | None:
    """Map explicit availability text/URLs to schema.org values. Never guesses."""
    if not raw:
        return None
    s = str(raw).strip()
    tail = s.rsplit("/", 1)[-1].lower()
    if tail in SCHEMA_AVAILABILITY:
        return SCHEMA_AVAILABILITY[tail]
    for pat, value in AVAILABILITY_MAP:
        if pat.search(s):
            return value
    return None


def split_value_unit(raw: str | None) -> tuple[str | None, str | None]:
    """'12V' -> ('12', 'V'); '3000 RPM' -> ('3000', 'RPM'); keeps original text if not numeric."""
    raw = clean_text(raw)
    if raw is None:
        return None, None
    m = UNIT_RE.match(raw)
    if m and m.group("unit"):
        unit_raw = m.group("unit")
        unit = KNOWN_UNITS.get(unit_raw.lower())
        if unit:
            return m.group("num").replace(" ", ""), unit
    return raw, None


def make_attribute(label: str, raw: str | None) -> ProductAttribute | None:
    label_c = clean_text(label, 200)
    if not label_c:
        return None
    value, unit = split_value_unit(raw)
    if value is None:
        return None
    return ProductAttribute(key=attribute_key(label_c), label=label_c, value=value, unit=unit, raw_value=clean_text(raw))


def parse_measurement(raw: str | None) -> Measurement | None:
    raw_c = clean_text(raw)
    if not raw_c:
        return None
    value, unit = split_value_unit(raw_c)
    return Measurement(value=value or raw_c, unit=unit, raw=raw_c)


def gtin_is_valid(code: str | None) -> bool:
    if not code or not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check


def normalize_gtin(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    return digits or None


def normalize_name_for_match(name: str | None) -> str:
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()
