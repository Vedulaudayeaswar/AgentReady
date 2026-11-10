"""Product page discovery: deterministic, explainable scoring.

Each signal contributes a weighted score; the reasons are stored on the
crawled page so merchants (and developers) can see why a page was or was
not treated as a product page. Pages in the ambiguous band can optionally be
sent to the AI classifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from app.services.extraction.html import ADD_TO_CART, PRICE_CLASS
from app.services.extraction.structured import parse_jsonld_blocks
from app.services.extraction.tables import iter_tables
from app.services.normalization.normalize import find_price_strings

PRODUCT_URL = re.compile(r"/(products?|item|items|p|dp|sku|shop/[^/]+|catalog(ue)?/[^/]+|store/[^/]+)(/|$)", re.I)
# Section roots only ("/shop", "/catalogue/") — "/catalogue/<product>/" is a product URL, not a listing.
LISTING_URL = re.compile(
    r"/(category|categories|collections?|range|price-?list)(/|$)|/(shop|catalog(ue)?|products)/?(index\.html?)?$|[?&]page=",
    re.I,
)
# Secondary sections that show other products on a product page (they must not turn it into a listing).
SECONDARY_SECTION = re.compile(
    r"(related|recent(ly)?[-_ ]?view|recommend|also[-_ ]?(like|bought)|upsell|cross[-_ ]?sell|similar|carousel|you[-_ ]may)",
    re.I,
)
NEGATIVE_URL = re.compile(
    r"/(blog|news|about|contact|careers?|jobs|privacy|terms|policy|faq|help|login|signin|sign-in|register|"
    r"account|cart|checkout|wishlist|search|tag|author|press|sitemap)(/|$|\.)",
    re.I,
)
SKU_TEXT = re.compile(r"\b(SKU|Item code|Part No|Model No|MPN|EAN|UPC)\b", re.I)

PRODUCT_THRESHOLD = 5.0
LISTING_THRESHOLD = 4.0
AMBIGUOUS_BAND = (2.5, 5.0)


@dataclass
class Classification:
    page_type: str  # product | listing | other
    score: float
    listing_score: float
    reasons: list[str] = field(default_factory=list)
    ambiguous: bool = False


def _primary_content(soup: BeautifulSoup):
    """The page body without asides and related/recently-viewed product sections.

    Removal is in place: those sections never contain the products a page is about, and
    removing them also keeps extraction from picking up related items' prices and images.
    """
    body = soup.body or soup
    for el in body.find_all(["aside"]):
        el.decompose()
    # 1. containers explicitly marked as secondary (class/id)
    for el in body.find_all(["section", "div", "ul", "ol"]):
        if el.decomposed:
            continue
        marker = " ".join(el.get("class") or []) + " " + (el.get("id") or "")
        if SECONDARY_SECTION.search(marker):
            el.decompose()
    # 2. sections introduced by a heading such as "Products you recently viewed" / "Related products":
    #    the heading (or its small wrapper) and everything after it up to the next heading of the same level
    for h in body.find_all(["h2", "h3", "h4"]):
        if h.decomposed or not SECONDARY_SECTION.search(h.get_text(" ", strip=True)):
            continue
        anchor = h
        if h.parent is not None and h.parent is not body and len(h.parent.find_all(True)) <= 3:
            anchor = h.parent  # e.g. <div class="sub-header"><h2>…</h2></div>
        for sib in list(anchor.find_next_siblings()):
            if sib.name == h.name or sib.find(h.name) is not None and sib.find(h.name).find_parent("article") is None:
                break
            sib.decompose()
        anchor.decompose()
    return body


def classify_page(url: str, soup: BeautifulSoup, meta: dict[str, str]) -> Classification:
    score = 0.0
    listing = 0.0
    reasons: list[str] = []

    def add(points: float, why: str, to_listing: bool = False):
        nonlocal score, listing
        if to_listing:
            listing += points
        else:
            score += points
        reasons.append(f"{'+' if points >= 0 else ''}{points:g} {'listing: ' if to_listing else ''}{why}")

    path = re.sub(r"^https?://[^/]+", "", url)
    if NEGATIVE_URL.search(path):
        add(-4, "non-product URL section")
        add(-4, "non-product URL section", True)
    if PRODUCT_URL.search(path):
        add(2, "product-like URL")
    if LISTING_URL.search(path):
        add(1.5, "listing-like URL", True)

    jsonld = parse_jsonld_blocks(soup)
    if len(jsonld) == 1:
        add(6, "JSON-LD Product")
    elif len(jsonld) > 1:
        add(6, f"{len(jsonld)} JSON-LD Products", True)
    if soup.find(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)}):
        n = len(soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)}))
        add(4, "microdata Product") if n == 1 else add(4, f"{n} microdata Products", True)
    if meta.get("og:type", "").lower().startswith("product"):
        add(3, "og:type=product")
    if "product:price:amount" in meta:
        add(2, "product price meta tag")

    body = _primary_content(soup)
    text = body.get_text(" ", strip=True)[:50000]
    prices = find_price_strings(text)
    if prices:
        add(1.5 if len(prices) <= 3 else 0.5, f"{len(prices)} price string(s)")
    if len(prices) >= 4:
        add(2, "many prices", True)
    # Repeated purchase buttons / price elements indicate a listing, not one product.
    price_elements = len(body.find_all(True, attrs={"class": PRICE_CLASS}))
    if price_elements >= 4:
        add(2, f"{price_elements} price elements", True)
    elif price_elements:
        add(1, "price element")
    ctas = len(ADD_TO_CART.findall(text))
    if ctas >= 3:
        add(3, f"{ctas} purchase buttons", True)
    elif ctas:
        add(2, "purchase call-to-action")
    if SKU_TEXT.search(text):
        add(1, "product identifier label")
    if body.find("h1") and len(body.find_all("h1")) == 1:
        add(0.5, "single H1")

    product_tables = 0
    for _, analysis in iter_tables(soup):
        if analysis.kind == "product":
            product_tables += 1
        elif analysis.kind == "spec":
            add(1.5, "specification table")
    if product_tables:
        add(5, f"{product_tables} product table(s)", True)

    cards = body.find_all(["div", "li", "article"], attrs={"class": re.compile(r"product", re.I)})
    if len(cards) >= 3 and len(prices) >= 3:
        add(3, f"{len(cards)} repeating product cards", True)

    if listing >= LISTING_THRESHOLD and listing >= score:
        page_type = "listing"
    elif score >= PRODUCT_THRESHOLD:
        page_type = "product"
    else:
        page_type = "other"
    ambiguous = page_type == "other" and AMBIGUOUS_BAND[0] <= max(score, listing) < AMBIGUOUS_BAND[1]
    return Classification(page_type, round(score, 2), round(listing, 2), reasons, ambiguous)
