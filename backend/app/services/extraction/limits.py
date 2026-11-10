"""Cheap structural limits checked before any expensive HTML processing.

A small but pathological page (e.g. tens of thousands of nested elements) can make
tree-walking code quadratic and stall a scan worker for minutes. lxml's C-level
event iteration measures size and depth in milliseconds, so oversized pages are
skipped with a warning instead.
"""

from __future__ import annotations

from lxml import etree

MAX_DOM_ELEMENTS = 60_000
MAX_DOM_DEPTH = 200


class PageTooComplex(ValueError):
    pass


def check_page_complexity(html: str) -> tuple[int, int]:
    """Return (elements, max_depth); raise PageTooComplex when over the limits."""
    parser = etree.HTMLParser(recover=True, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(html.encode("utf-8", "replace"), parser)
    except (etree.XMLSyntaxError, ValueError):
        return 0, 0
    if root is None:
        return 0, 0
    elements = depth = max_depth = 0
    for event, _ in etree.iterwalk(root, events=("start", "end")):
        if event == "start":
            elements += 1
            depth += 1
            if depth > max_depth:
                max_depth = depth
            if elements > MAX_DOM_ELEMENTS:
                raise PageTooComplex(f"page has more than {MAX_DOM_ELEMENTS} elements")
            if max_depth > MAX_DOM_DEPTH:
                raise PageTooComplex(f"page nesting is deeper than {MAX_DOM_DEPTH} levels")
        else:
            depth -= 1
    return elements, max_depth
