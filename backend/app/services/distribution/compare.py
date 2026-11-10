"""Attribute-aware product comparison built only from public product representations.

* Values in different currencies are shown side by side, never converted or ranked against each other.
* Numeric attributes are compared only when every known value converts to the same dimension through the
  explicit rules in ``units``; otherwise the row is marked ``not_comparable`` with the reason.
* Missing values are ``unknown`` — never assumed.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.commerce.catalog_rules import canonical_key
from app.services.distribution.units import display, parse_quantity


def _price(pub: dict):
    p = pub.get("price")
    if not p:
        return None
    return {"current": p["current"], "regular": p["regular"], "on_sale": p["on_sale"], "currency": p["currency"]}


def _numeric_row(values: dict[str, dict | None]) -> dict:
    known = {pid: v for pid, v in values.items() if v is not None}
    qs = {pid: parse_quantity(v["value"], v.get("unit")) for pid, v in known.items()}
    if known and all(q is not None for q in qs.values()):
        dims = {q.dimension for q in qs.values()}
        if len(dims) == 1:
            ordered = sorted(qs.items(), key=lambda kv: kv[1].value)
            lo, hi = ordered[0][1].value, ordered[-1][1].value
            return {"comparable": True, "kind": "quantity", "unit": ordered[0][1].unit,
                    "normalized": {pid: display(q) for pid, q in qs.items()},
                    "lowest": [pid for pid, q in qs.items() if q.value == lo],
                    "highest": [pid for pid, q in qs.items() if q.value == hi], "all_equal": lo == hi}
        units = sorted({(v.get("unit") or "?") for v in known.values()})
        return {"comparable": False, "kind": "quantity", "reason": f"Incompatible units ({', '.join(units)}) — no conversion rule"}
    if known and any(q is not None for q in qs.values()):
        return {"comparable": False, "kind": "mixed", "reason": "Some values are not numeric or use an unknown unit"}
    texts = {pid: str(v["value"]).strip().lower() for pid, v in known.items()}
    return {"comparable": bool(known), "kind": "text", "all_equal": len(set(texts.values())) <= 1 if known else None}


def compare(pubs: list[dict]) -> dict:
    ids = [p["id"] for p in pubs]
    rows: list[dict] = []

    prices = {p["id"]: _price(p) for p in pubs}
    currencies = {v["currency"] for v in prices.values() if v}
    price_row: dict = {"field": "price", "label": "Price", "values": prices}
    if len(currencies) > 1:
        price_row.update(comparable=False, reason=f"Different currencies ({', '.join(sorted(currencies))}); prices are not converted")
    elif currencies:
        known = {pid: Decimal(v["current"]) for pid, v in prices.items() if v}
        lo = min(known.values())
        price_row.update(comparable=True, currency=next(iter(currencies)), lowest=[pid for pid, a in known.items() if a == lo],
                         unknown=[pid for pid, v in prices.items() if v is None])
    else:
        price_row.update(comparable=False, reason="No product has a public price")
    rows.append(price_row)

    for field, label, get in (
        ("brand", "Brand", lambda p: p.get("brand")),
        ("availability", "Availability", lambda p: (p.get("availability") or {}).get("status")),
        ("merchant", "Merchant", lambda p: p["merchant"]["name"]),
        ("category", "Category", lambda p: (p.get("category") or {}).get("path")),
        ("shipping_lead_time_days", "Shipping lead time (days)", lambda p: (p.get("shipping") or {}).get("lead_time_days")),
        ("material", "Material", lambda p: (p.get("specifications") or {}).get("material")),
    ):
        vals = {p["id"]: get(p) for p in pubs}
        rows.append({"field": field, "label": label, "values": {k: (v if v not in (None, "") else "unknown") for k, v in vals.items()},
                     "unknown": [k for k, v in vals.items() if v in (None, "")]})

    for spec in ("weight",):
        vals = {}
        for p in pubs:
            w = (p.get("specifications") or {}).get(spec)
            vals[p["id"]] = {"value": str(w.get("value")), "unit": w.get("unit")} if isinstance(w, dict) and w.get("value") else None
        row = {"field": spec, "label": spec.title(), "values": {k: v or "unknown" for k, v in vals.items()},
               "unknown": [k for k, v in vals.items() if v is None]}
        row.update(_numeric_row(vals))
        rows.append(row)

    # attributes: union of canonical keys
    by_key: dict[str, dict] = {}
    for p in pubs:
        for a in p["attributes"]:
            k = canonical_key(a["key"])
            entry = by_key.setdefault(k, {"label": a["name"], "values": {}})
            entry["values"].setdefault(p["id"], {"value": a["value"], "unit": a["unit"]})
    for k, entry in sorted(by_key.items()):
        vals = {pid: entry["values"].get(pid) for pid in ids}
        row = {"field": f"attribute:{k}", "label": entry["label"],
               "values": {pid: (v if v is not None else "unknown") for pid, v in vals.items()},
               "unknown": [pid for pid, v in vals.items() if v is None]}
        row.update(_numeric_row(vals))
        rows.append(row)

    variants = {}
    for p in pubs:
        opts: dict[str, set] = {}
        for v in p["variants"]:
            for name, val in (v["options"] or {}).items():
                opts.setdefault(name, set()).add(str(val))
        variants[p["id"]] = {"count": len(p["variants"]), "options": {k: sorted(v) for k, v in opts.items()}}
    rows.append({"field": "variants", "label": "Variants", "values": variants})

    return {"products": [{"id": p["id"], "name": p["name"], "merchant": p["merchant"], "url": p["url"]} for p in pubs],
            "rows": rows,
            "rules": {"currency": "never converted", "units": "converted only with explicit rules (data, mass, length, "
                      "voltage, current, power, volume, charge, frequency, pressure)", "missing": "reported as unknown"}}
