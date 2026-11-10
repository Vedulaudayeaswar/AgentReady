"""Turn HTML into compact, model-friendly text (to minimise tokens).

Only the main content region is used; scripts, styles, navigation, footers
and forms are removed; tables become pipe-delimited rows; images are listed
separately so the model can only reference real image URLs.
"""

from __future__ import annotations

import copy
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from app.services.extraction.html import main_region
from app.services.extraction.tables import build_grid

REMOVE = ["script", "style", "noscript", "svg", "iframe", "object", "embed", "template", "canvas", "form",
          "input", "select", "button", "nav", "footer", "link", "meta"]


def html_to_text(region: Tag) -> str:
    region = copy.copy(region)
    for t in region.find_all(REMOVE):
        t.decompose()
    for el in region.find_all(attrs={"aria-hidden": "true"}):
        el.decompose()
    for table in region.find_all("table")[::-1]:
        grid, _, _ = build_grid(table)
        txt = "\n".join(" | ".join(r) for r in grid)
        table.replace_with(NavigableString(f"\n{txt}\n"))
    for h in region.find_all(re.compile(r"^h[1-6]$")):
        level = int(h.name[1])
        h.replace_with(NavigableString(f"\n{'#' * level} {h.get_text(' ', strip=True)}\n"))
    for li in region.find_all("li"):
        li.insert(0, NavigableString("- "))
    text = region.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def list_images(region: Tag, page_url: str, limit: int = 10) -> list[tuple[str, str]]:
    out = []
    for img in region.find_all("img"):
        src = img.get("data-src") or img.get("src")
        if not src or src.startswith("data:"):
            continue
        out.append((urljoin(page_url, src), (img.get("alt") or "").strip()))
        if len(out) >= limit:
            break
    return out


def prepare_page_content(soup: BeautifulSoup, page_url: str, max_chars: int) -> tuple[str, list[str]]:
    region = main_region(soup)
    text = html_to_text(region)
    images = list_images(region, page_url)
    img_block = "\n".join(f"IMAGE {u}" + (f" (alt: {a})" if a else "") for u, a in images)
    budget = max_chars - len(img_block) - 20
    if len(text) > budget:
        text = text[: max(budget, 0)] + "\n[content truncated]"
    content = text + ("\n\nIMAGES:\n" + img_block if img_block else "")
    return content, [u for u, _ in images]


def prepare_table_content(table_text: str, context: str | None, max_chars: int) -> str:
    head = f"Context: {context}\n" if context else ""
    return (head + table_text)[:max_chars]
