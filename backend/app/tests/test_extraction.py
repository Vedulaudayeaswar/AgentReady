from decimal import Decimal

from bs4 import BeautifulSoup

from app.services.discovery.classifier import classify_page
from app.services.extraction.pipeline import PageExtractor
from app.services.extraction.structured import parse_meta
from app.services.extraction.tables import analyse_table, products_from_table
from app.tests.sitekit import FIXTURES

SHOP = FIXTURES / "shop"
BASE = "https://shop.example"


def html(name: str) -> str:
    return (SHOP / name).read_text(encoding="utf-8")


def tables(markup: str):
    soup = BeautifulSoup(markup, "lxml")
    return [(t, analyse_table(t)) for t in soup.find_all("table")]


# ------------------------------------------------------------------ tables
def test_motor_table_with_merged_cells_and_unit_headers():
    (t, a), *_ = tables(html("price-list.html"))
    assert a.kind == "product"
    products = products_from_table(a, t, f"{BASE}/price-list")
    names = [p.name for p in products]
    assert names == ["Motor A", "Motor B", "Steel Bolt M10"]
    a_, b_ = products[0], products[1]
    assert a_.price.amount == Decimal("450.00") and a_.price.currency == "INR"  # currency from "Price (INR)" header
    assert {x.key: (x.value, x.unit) for x in a_.attributes} == {"voltage": ("12", "V"), "rpm": ("3000", None)}
    assert b_.category == "DC Motors"  # rowspan propagated
    ev = [e for e in a_.evidence if e.field == "price"][0]
    assert ev.source_type == "html_table" and ev.source_value == "450" and "Motor A" in ev.source_fragment


def test_unrelated_and_headerless_tables():
    results = [a.kind for _, a in tables(html("price-list.html"))]
    assert results[1] == "spec"  # opening hours: 2-col label/value -> never a product table
    assert results[2] == "ambiguous"  # headerless relay table: needs AI, not guessed


def test_inconsistent_headers_and_nested_tables():
    markup = """<table><tr><th>Item Code</th><th>Description of goods</th><th>Rate</th><th>Wt</th></tr>
    <tr><td>A-1</td><td>Brass valve<table><tr><td>nested note</td></tr></table></td><td>$12.50</td><td>2 kg</td></tr></table>"""
    (t, a), *_ = tables(markup)
    assert a.kind == "product" and set(a.column_map.values()) >= {"sku", "name", "price", "weight"}
    [p] = products_from_table(a, t, BASE)
    assert p.name == "Brass valve" and p.sku == "A-1"
    assert p.price.amount == Decimal("12.50") and p.price.currency == "USD" and p.price.currency_inferred
    assert p.weight.value == "2" and p.weight.unit == "kg"


# ------------------------------------------------------------------ classification
def classify(name: str, path: str):
    soup = BeautifulSoup(html(name), "lxml")
    return classify_page(BASE + path, soup, parse_meta(soup))


def test_classifier():
    assert classify("dc-motor-12v.html", "/products/dc-motor-12v").page_type == "product"
    assert classify("steel-bolt-m10.html", "/products/steel-bolt-m10").page_type == "product"
    assert classify("price-list.html", "/price-list").page_type == "listing"
    assert classify("about.html", "/about").page_type == "other"
    assert classify("blog.html", "/blog/new-arrivals").page_type == "other"
    assert classify("index.html", "/").page_type == "other"


# ------------------------------------------------------------------ page extraction
async def extract(name: str, path: str):
    return (await PageExtractor(None).extract(BASE + path, html(name))).result.products


async def test_jsonld_page_with_spec_table():
    [p] = await extract("dc-motor-12v.html", "/products/dc-motor-12v")
    assert (p.name, p.sku, p.brand, p.gtin) == ("12V DC Motor", "MTR001", "VoltPro", "4006381333931")
    assert p.price.amount == Decimal("450.00") and p.price.currency == "INR"
    assert p.availability == "InStock"
    attrs = {a.key: (a.value, a.unit) for a in p.attributes}
    assert attrs["voltage"] == ("12", "V") and attrs["speed"] == ("3000", "RPM")
    assert not [f for f in p.flags if f["code"] == "conflicting_values"]  # JSON-LD 450 == visible Rs. 450
    price_ev = [e for e in p.evidence if e.field == "price"]
    assert price_ev[0].source_type == "json_ld" and price_ev[0].extraction_method == "deterministic"


async def test_plain_html_page_ignores_struck_through_price():
    [p] = await extract("steel-bolt-m10.html", "/products/steel-bolt-m10")
    assert p.name == "Steel Bolt M10" and p.sku == "BLT-M10" and p.brand == "FastenRight"
    assert p.price.amount == Decimal("25.00") and p.price.currency == "INR"
    assert p.availability == "InStock"
    assert {a.key for a in p.attributes} >= {"thread", "length"}


async def test_conflicting_prices_are_flagged_not_hidden():
    [p] = await extract("heavy-motor.html", "/products/heavy-motor")
    assert p.price.amount == Decimal("999.00")  # most trusted source (JSON-LD) wins
    conflict = [f for f in p.flags if f["code"] == "conflicting_values" and f["field"] == "price"]
    assert conflict and set(conflict[0]["values"]) == {"999.00", "1099.00"}


async def test_missing_price_stays_missing():
    [p] = await extract("gear-pump.html", "/products/gear-pump")
    assert p.name == "Gear Pump GP-200" and p.price is None and p.availability is None
    assert p.sku == "GP-200"


async def test_malformed_html():
    [p] = await extract("broken-page.html", "/products/broken-page")
    assert p.name.startswith("Copper Wire 2.5mm")
    assert p.price.amount == Decimal("1250.00")
    assert p.availability == "OutOfStock"


async def test_js_only_page_yields_nothing_without_renderer():
    assert await extract("app.html", "/app") == []


CATEGORY_LISTING = """<html><head><title>Travel | Books</title></head><body>
<div class="page-header"><h1>Travel</h1></div><ol class="row">""" + "".join(
    f"""<li class="col"><article class="product_pod"><div class="image_container"><a href="../../item-{i}/index.html">
    <img src="/media/{i}.jpg" alt="A Very Long Book Title Number {i}"></a></div>
    <h3><a href="../../item-{i}/index.html" title="A Very Long Book Title Number {i}">A Very Long Book ...</a></h3>
    <div class="product_price"><p class="price_color">£{40 + i}.50</p><p class="instock availability">In stock</p>
    <form><button type="submit">Add to basket</button></form></div></article></li>"""
    for i in range(6)
) + "</ol></body></html>"


async def test_category_listing_with_cards_is_not_a_single_product():
    """Regression (found scanning a live site): a category page with an H1 and many priced
    cards was misread as one product named after the category."""
    url = "https://books.example/catalogue/category/books/travel_2/index.html"
    outcome = await PageExtractor(None).extract(url, CATEGORY_LISTING)
    assert outcome.classification.page_type == "listing"
    names = [p.name for p in outcome.result.products]
    assert "Travel" not in names
    assert names[0] == "A Very Long Book Title Number 0"  # full title, not the truncated text
    p = outcome.result.products[0]
    assert str(p.price.amount) == "40.50" and p.price.currency == "GBP" and p.availability == "InStock"
    assert p.product_url == "https://books.example/catalogue/category/item-0/index.html"


def test_microdata_name_on_link_uses_text_not_href():
    # Common shop markup: itemprop="name" on the product link, visible text truncated, full name in title=.
    from app.services.extraction.structured import parse_microdata

    html = """<div itemscope itemtype="https://schema.org/Product">
      <a href="/shop/product/60" class="title" title="Asus VivoBook X441NA-GA190" itemprop="name">Asus VivoBook...</a>
      <a href="/shop/product/60" itemprop="url">details</a>
      <img itemprop="image" src="/img/60.png">
      <span itemprop="price">$295.99</span><meta itemprop="priceCurrency" content="USD">
    </div>
    <div itemscope itemtype="https://schema.org/Product">
      <a href="/shop/product/61" itemprop="name">Lenovo ThinkPad E14</a><span itemprop="price">$500</span>
    </div>"""
    products = parse_microdata(BeautifulSoup(html, "lxml"), "https://shop.example/laptops")
    assert [p.name for p in products] == ["Asus VivoBook X441NA-GA190", "Lenovo ThinkPad E14"]
    assert products[0].images[0].url == "https://shop.example/img/60.png"
    assert products[0].price.amount == Decimal("295.99")


def test_duplicate_issues_are_capped_per_product():
    import uuid
    from types import SimpleNamespace

    from app.services.deduplication.dedupe import find_duplicates
    from app.services.validation import validator

    same = [SimpleNamespace(id=uuid.uuid4(), name="Widget Pro 3000", sku=None, gtin=None, brand=None,
                            product_url=None, source_url=None) for _ in range(40)]
    assert len(find_duplicates(same)) == 40 * 39 // 2  # every pair is a candidate ...
    assert validator.MAX_DUPLICATE_ISSUES_PER_PRODUCT == 5  # ... but revalidate_business keeps 5 per product


def test_html_name_prefers_product_markup_over_site_banner_h1():
    from app.services.extraction.html import heuristic_product

    html = """<html><body><h1>Test Sites</h1><div itemscope itemtype="https://schema.org/Product">
      <h4 class="title" itemprop="name">Asus VivoBook X441NA-GA190</h4><span class="price">$295.99</span>
    </div></body></html>"""
    p = heuristic_product(BeautifulSoup(html, "lxml"), "https://shop.example/product/60", {})
    assert p is not None and p.name == "Asus VivoBook X441NA-GA190"
