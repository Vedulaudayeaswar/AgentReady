"""Cross-catalog duplicate candidate detection.

Never merges anything: it only proposes pairs for human review.
Blocking keeps it near-linear: candidates are compared only within shared
SKU / GTIN / URL / name-token blocks.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

from app.services.normalization.normalize import normalize_name_for_match

NAME_SIMILARITY_THRESHOLD = 0.92


class _P(Protocol):
    id: uuid.UUID
    name: str | None
    sku: str | None
    gtin: str | None
    brand: str | None
    product_url: str | None
    source_url: str | None


@dataclass(frozen=True)
class DuplicatePair:
    a: uuid.UUID
    b: uuid.UUID
    reason: str
    score: float


def _key(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) < str(b) else (b, a)


def find_duplicates(products: list[_P]) -> list[DuplicatePair]:
    pairs: dict[tuple, DuplicatePair] = {}

    def add(a: _P, b: _P, reason: str, score: float):
        if a.id == b.id:
            return
        k = _key(a.id, b.id)
        if k not in pairs or pairs[k].score < score:
            pairs[k] = DuplicatePair(k[0], k[1], reason, score)

    blocks: dict[str, list[_P]] = defaultdict(list)
    for p in products:
        if p.sku:
            blocks[f"sku:{p.sku.strip().lower()}"].append(p)
        if p.gtin:
            blocks[f"gtin:{p.gtin}"].append(p)
        if p.product_url and p.product_url != p.source_url:
            blocks[f"url:{p.product_url}"].append(p)
        n = normalize_name_for_match(p.name)
        if n:
            blocks[f"name:{n.split(' ')[0]}"].append(p)

    for key, items in blocks.items():
        if len(items) < 2 or len(items) > 400:
            continue
        kind = key.split(":", 1)[0]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if kind == "sku":
                    add(a, b, "Same SKU", 1.0)
                elif kind == "gtin":
                    add(a, b, "Same GTIN", 1.0)
                elif kind == "url":
                    add(a, b, "Same product URL", 0.95)
                else:
                    if a.sku and b.sku and a.sku.strip().lower() != b.sku.strip().lower():
                        continue  # distinct SKUs => distinct products
                    if a.gtin and b.gtin and a.gtin != b.gtin:
                        continue
                    if a.brand and b.brand and a.brand.strip().lower() != b.brand.strip().lower():
                        continue
                    na, nb = normalize_name_for_match(a.name), normalize_name_for_match(b.name)
                    ratio = 1.0 if na == nb else SequenceMatcher(None, na, nb).ratio()
                    if ratio >= NAME_SIMILARITY_THRESHOLD:
                        add(a, b, "Identical name" if ratio == 1.0 else f"Similar name ({ratio:.0%})", round(0.9 * ratio, 3))
    return list(pairs.values())
