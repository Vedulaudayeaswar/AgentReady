"""Specialised HTML table extraction.

* builds a rectangular grid honouring rowspan/colspan (merged cells);
* ignores the contents of nested tables when reading an outer cell and
  processes nested tables separately;
* detects header rows (``<thead>``, ``<th>`` rows, or a textual first row
  above numeric rows) and maps inconsistent column names onto canonical
  fields via synonyms;
* classifies each table as ``product`` (one product per row), ``spec``
  (key/value specification table), ``ambiguous`` (looks product-like but
  could not be mapped deterministically -> AI candidate) or ``unrelated``.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.schemas.canonical import ExtractedProduct, ProductEvidence, ProductImage
from app.services.normalization.normalize import (
    clean_text,
    detect_currency,
    find_price_strings,
    make_attribute,
    normalize_availability,
    normalize_gtin,
    parse_measurement,
    parse_price,
)

SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("product", "product name", "item", "item name", "name", "title", "model", "description of goods",
             "product description", "part name", "particulars", "article name", "product title"),
    "sku": ("sku", "code", "item code", "product code", "part no", "part number", "part #", "model no",
            "model number", "cat no", "catalog no", "catalogue no", "article no", "ref", "reference", "item no",
            "item #", "product id", "item id"),
    "price": ("price", "mrp", "rate", "cost", "unit price", "amount", "selling price", "our price", "list price",
              "offer price", "price per unit", "sale price", "retail price"),
    "currency": ("currency", "curr"),
    "brand": ("brand", "make", "manufacturer brand"),
    "manufacturer": ("manufacturer", "mfr", "maker"),
    "category": ("category", "type", "product type", "group", "family"),
    "availability": ("availability", "stock", "in stock", "status", "stock status"),
    "description": ("description", "details", "remarks", "features", "notes"),
    "gtin": ("gtin", "ean", "upc", "barcode", "ean13", "isbn"),
    "mpn": ("mpn", "manufacturer part number", "mfr part no"),
    "image": ("image", "photo", "picture", "img"),
    "material": ("material",),
    "weight": ("weight", "net weight", "wt"),
}
_UNIT_IN_HEADER = re.compile(r"\(([^)]{1,15})\)\s*$|\[([^\]]{1,15})\]\s*$")
_NUM = re.compile(r"^[\s\d.,%+\-x×/:₹$€£]+[A-Za-z]{0,4}$")


@dataclass
class TableAnalysis:
    kind: str  # product | spec | ambiguous | unrelated
    header: list[str] | None
    rows: list[list[str]]
    column_map: dict[int, str] = field(default_factory=dict)
    text: str = ""
    index: int = 0


def _direct_rows(table: Tag) -> list[Tag]:
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _cell_text(cell: Tag) -> str:
    if cell.find("table"):
        cell = copy.copy(cell)
        for nested in cell.find_all("table"):
            nested.decompose()
    return clean_text(cell.get_text(" ", strip=True)) or ""


def build_grid(table: Tag) -> tuple[list[list[str]], list[list[Tag | None]], list[bool]]:
    """Return (text grid, cell-element grid, is_header_row flags)."""
    grid: list[list[str]] = []
    cells_grid: list[list[Tag | None]] = []
    header_flags: list[bool] = []
    pending: dict[tuple[int, int], tuple[str, Tag]] = {}
    for r, tr in enumerate(_direct_rows(table)):
        row: list[str] = []
        row_cells: list[Tag | None] = []
        c = 0
        cells = [td for td in tr.find_all(["td", "th"]) if td.find_parent("tr") is tr]
        in_thead = tr.find_parent("thead") is not None
        header_flags.append(in_thead or (bool(cells) and all(td.name == "th" for td in cells)))
        for td in cells:
            while (r, c) in pending:
                t, el = pending.pop((r, c))
                row.append(t)
                row_cells.append(el)
                c += 1
            text = _cell_text(td)
            try:
                colspan = max(1, min(int(td.get("colspan", 1)), 50))
                rowspan = max(1, min(int(td.get("rowspan", 1)), 200))
            except ValueError:
                colspan, rowspan = 1, 1
            for k in range(colspan):
                row.append(text)
                row_cells.append(td)
                for rr in range(1, rowspan):
                    pending[(r + rr, c + k)] = (text, td)
            c += colspan
        while (r, c) in pending:
            t, el = pending.pop((r, c))
            row.append(t)
            row_cells.append(el)
            c += 1
        if any(x for x in row):
            grid.append(row)
            cells_grid.append(row_cells)
        else:
            header_flags.pop()
    width = max((len(r) for r in grid), default=0)
    for row, cells in zip(grid, cells_grid):
        row.extend([""] * (width - len(row)))
        cells.extend([None] * (width - len(cells)))
    return grid, cells_grid, header_flags


def _is_numericish(s: str) -> bool:
    return bool(s) and bool(_NUM.match(s))


def _match_header(h: str) -> str | None:
    hl = re.sub(r"[\s_]+", " ", _UNIT_IN_HEADER.sub("", h).lower()).strip(" :#.")
    if not hl:
        return None
    for fieldname, syns in SYNONYMS.items():
        if hl in syns:
            return fieldname
    for fieldname, syns in SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(s)}\b", hl) for s in syns if len(s) > 3):
            return fieldname
    return None


def analyse_table(table: Tag, index: int = 0) -> TableAnalysis:
    grid, _, hflags = build_grid(table)
    text = "\n".join(" | ".join(r) for r in grid)[:4000]
    if len(grid) < 2 or max(len(r) for r in grid) < 2:
        return TableAnalysis("unrelated", None, grid, text=text, index=index)

    width = len(grid[0])
    # ---- spec table: two columns of label/value
    if width == 2 and not any(hflags[1:]):
        labels = [r[0] for r in grid]
        if sum(1 for l in labels if l and not _is_numericish(l)) >= max(2, int(len(labels) * 0.7)):
            first_is_header = hflags[0] and _match_header(grid[0][0]) is None and not find_price_strings(grid[0][1])
            rows = grid[1:] if first_is_header and grid[0][0].lower() in ("specification", "feature", "property", "attribute", "parameter") else grid
            return TableAnalysis("spec", None, rows, text=text, index=index)

    # ---- header detection
    header_idx = None
    if hflags[0]:
        header_idx = 0
    else:
        first_textual = sum(1 for x in grid[0] if x and not _is_numericish(x)) == len([x for x in grid[0] if x])
        rest_numeric = any(_is_numericish(x) for row in grid[1:4] for x in row)
        mapped = sum(1 for x in grid[0] if _match_header(x))
        if first_textual and (rest_numeric or mapped >= 2):
            header_idx = 0
    header = grid[header_idx] if header_idx is not None else None
    rows = grid[header_idx + 1 :] if header_idx is not None else grid

    if header is None:
        price_cols = sum(1 for row in rows for x in row if find_price_strings(x))
        kind = "ambiguous" if price_cols >= 2 or (len(rows) >= 2 and width >= 3) else "unrelated"
        return TableAnalysis(kind, None, rows, text=text, index=index)

    column_map: dict[int, str] = {}
    used: set[str] = set()
    for i, h in enumerate(header):
        f = _match_header(h)
        if f and f not in used:
            column_map[i] = f
            used.add(f)
    if "name" not in used:
        # fall back: "model"/"sku" column can act as the name when no name column exists
        for i, f in list(column_map.items()):
            if f == "sku":
                column_map[i] = "name"
                used.discard("sku")
                used.add("name")
                break
    if "name" not in used:
        textual_cols = [i for i in range(width) if i not in column_map and all(not _is_numericish(r[i]) for r in rows if r[i])]
        if textual_cols and ("price" in used or len(header) >= 3):
            column_map[textual_cols[0]] = "name"
            used.add("name")
    if "name" in used and (("price" in used) or ("sku" in used) or len(header) - len(column_map) >= 1) and rows:
        return TableAnalysis("product", header, rows, column_map=column_map, text=text, index=index)
    if len(rows) >= 2 and width >= 3:
        return TableAnalysis("ambiguous", header, rows, text=text, index=index)
    return TableAnalysis("unrelated", header, rows, text=text, index=index)


def _header_unit(h: str) -> str | None:
    m = _UNIT_IN_HEADER.search(h)
    return (m.group(1) or m.group(2)).strip() if m else None


def _ev(field: str, value, raw: str, url: str, fragment: str) -> ProductEvidence:
    return ProductEvidence(
        field=field, value=value, source_value=raw[:500], source_url=url, source_fragment=fragment[:1500],
        source_type="html_table", extraction_method="deterministic", confidence=0.8, verified_in_source=True,
    )


def products_from_table(analysis: TableAnalysis, table: Tag, page_url: str) -> list[ExtractedProduct]:
    if analysis.kind != "product" or not analysis.header:
        return []
    header = analysis.header
    grid, cells, _ = build_grid(table)
    # align element grid to data rows (rows after header)
    start = len(grid) - len(analysis.rows)
    cells = cells[start:]
    price_currency_hint = None
    for i, f in analysis.column_map.items():
        if f == "price":
            unit = _header_unit(header[i])
            if unit:
                price_currency_hint = detect_currency(unit)[0] or (unit.upper() if re.fullmatch(r"[A-Za-z]{3}", unit) else None)
    out: list[ExtractedProduct] = []
    for r, row in enumerate(analysis.rows):
        if all(not x for x in row):
            continue
        # skip repeated header rows inside the body
        if [x.lower() for x in row] == [h.lower() for h in header]:
            continue
        fragment = " | ".join(header) + "\n" + " | ".join(row)
        p = ExtractedProduct(origin="html_table", source_url=page_url)
        for i, val in enumerate(row):
            if not val:
                continue
            f = analysis.column_map.get(i)
            el = cells[r][i] if r < len(cells) else None
            if f == "name":
                p.name = clean_text(val, 500)
                p.evidence.append(_ev("name", p.name, val, page_url, fragment))
                a = el.find("a", href=True) if el is not None else None
                if a:
                    p.product_url = urljoin(page_url, a["href"])
                    p.evidence.append(_ev("product_url", p.product_url, a["href"], page_url, fragment))
            elif f == "price":
                price = parse_price(val, price_currency_hint)
                if price:
                    if price.currency is None and price_currency_hint:
                        price.currency = price_currency_hint
                    p.price = price
                    p.evidence.append(_ev("price", price.model_dump(mode="json"), val, page_url, fragment))
            elif f == "availability":
                av = normalize_availability(val)
                if av:
                    p.availability = av
                    p.evidence.append(_ev("availability", av, val, page_url, fragment))
            elif f == "gtin":
                p.gtin = normalize_gtin(val)
                p.evidence.append(_ev("gtin", p.gtin, val, page_url, fragment))
            elif f == "image":
                img = el.find("img") if el is not None else None
                if img and img.get("src"):
                    p.images.append(ProductImage(url=urljoin(page_url, img["src"]), alt=img.get("alt")))
            elif f == "weight":
                p.weight = parse_measurement(val + (f" {_header_unit(header[i])}" if _header_unit(header[i]) and not re.search(r"[A-Za-z]", val) else ""))
                p.evidence.append(_ev("weight", p.weight.model_dump() if p.weight else None, val, page_url, fragment))
            elif f in ("sku", "brand", "manufacturer", "category", "description", "mpn", "material", "currency"):
                if f == "currency":
                    if p.price:
                        p.price.currency = detect_currency(val)[0] or val.upper()[:3]
                    continue
                setattr(p, f, clean_text(val, 5000 if f == "description" else 300))
                p.evidence.append(_ev(f, getattr(p, f), val, page_url, fragment))
            else:
                label = header[i] or f"Column {i + 1}"
                unit = _header_unit(label)
                raw = val if (not unit or re.search(r"[A-Za-z]", val)) else f"{val} {unit}"
                attr = make_attribute(_UNIT_IN_HEADER.sub("", label).strip() or label, raw)
                if attr:
                    if unit and not attr.unit:
                        attr.unit = unit
                    attr.raw_value = val
                    p.attributes.append(attr)
                    p.evidence.append(_ev(f"attributes.{attr.key}", attr.value, val, page_url, fragment))
        if p.name:
            # images in the row even if there is no dedicated image column
            if not p.images and r < len(cells):
                for el in {id(c): c for c in cells[r] if c is not None}.values():
                    img = el.find("img", src=True)
                    if img:
                        p.images.append(ProductImage(url=urljoin(page_url, img["src"]), alt=img.get("alt")))
                        break
            out.append(p)
    return out


def spec_attributes(analysis: TableAnalysis, page_url: str):
    attrs, evidence = [], []
    for row in analysis.rows:
        label, value = row[0], row[1] if len(row) > 1 else ""
        attr = make_attribute(label, value)
        if attr:
            attrs.append(attr)
            evidence.append(
                ProductEvidence(
                    field=f"attributes.{attr.key}", value=attr.value, source_value=f"{label}: {value}"[:500],
                    source_url=page_url, source_fragment=analysis.text[:1500], source_type="html_spec",
                    extraction_method="deterministic", confidence=0.8, verified_in_source=True,
                )
            )
    return attrs, evidence


def iter_tables(soup: BeautifulSoup) -> list[tuple[Tag, TableAnalysis]]:
    out = []
    for i, t in enumerate(soup.find_all("table")):
        out.append((t, analyse_table(t, i)))
    return out
