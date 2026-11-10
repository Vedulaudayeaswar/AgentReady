from decimal import Decimal

import pytest

from app.services.crawler.urls import InvalidURL, normalize_url, validate_public_url_syntax
from app.services.normalization.normalize import (
    detect_currency,
    gtin_is_valid,
    normalize_availability,
    parse_price,
    split_value_unit,
)


@pytest.mark.parametrize("raw,amount,currency,inferred", [
    ("Rs. 450", "450.00", "INR", False),
    ("₹1,099", "1099.00", "INR", False),
    ("₹ 1,23,456.50", "123456.50", "INR", False),
    ("$12.99", "12.99", "USD", True),       # "$" is ambiguous -> inferred
    ("USD 12.99", "12.99", "USD", False),
    ("1.299,00 €", "1299.00", "EUR", False),
    ("12,50 EUR", "12.50", "EUR", False),
    ("450/-", "450.00", None, False),
    ("£5", "5.00", "GBP", False),
])
def test_parse_price(raw, amount, currency, inferred):
    p = parse_price(raw)
    assert p is not None
    assert p.amount == Decimal(amount)
    assert p.currency == currency
    assert p.currency_inferred is inferred
    assert p.raw == raw


def test_parse_price_never_invents():
    assert parse_price("Contact us for price") is None
    assert parse_price("") is None
    assert parse_price("450").currency is None  # no symbol -> no currency


def test_detect_currency_word_boundaries():
    assert detect_currency("Firmware v2") == (None, False)
    assert detect_currency("price: rs 20")[0] == "INR"


@pytest.mark.parametrize("raw,expected", [
    ("https://schema.org/InStock", "InStock"),
    ("In stock", "InStock"),
    ("Out of stock", "OutOfStock"),
    ("Pre-order now", "PreOrder"),
    ("Available in 3 colours", None),  # not availability information
    ("", None),
    (None, None),
])
def test_availability(raw, expected):
    assert normalize_availability(raw) == expected


@pytest.mark.parametrize("raw,value,unit", [
    ("12V", "12", "V"), ("3000 RPM", "3000", "RPM"), ("0.35 kg", "0.35", "kg"), ("6 mm", "6", "mm"),
    ("10x20 cm", "10x20", "cm"), ("Steel", "Steel", None), ("M10", "M10", None),
])
def test_split_value_unit_preserves_units(raw, value, unit):
    assert split_value_unit(raw) == (value, unit)


def test_gtin_checksum():
    assert gtin_is_valid("4006381333931")
    assert not gtin_is_valid("4006381333932")
    assert not gtin_is_valid("abc")


def test_url_normalization():
    assert normalize_url("HTTPS://Shop.Example:443/a//b?utm_source=x&b=2&a=1#frag") == "https://shop.example/a/b?a=1&b=2"
    assert normalize_url("/p/1", "https://shop.example/x/") == "https://shop.example/p/1"
    assert validate_public_url_syntax("shop.example") == "https://shop.example/"
    for bad in ("ftp://shop.example", "javascript:alert(1)", "https://user:pw@shop.example", ""):
        with pytest.raises(InvalidURL):
            validate_public_url_syntax(bad)
