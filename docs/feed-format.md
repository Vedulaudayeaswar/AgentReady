# AgentReady Normalized Product Feed — `agentready-feed/1.0`

A versioned JSON format for distributing an approved AgentReady catalog to downstream systems. It is produced by
`POST /api/v1/exports {"format": "feed"}` and returned (paginated) by `GET /api/v1/catalog`.

## Compatibility

* `feed_version` changes major version only for breaking changes. New optional fields may appear in minor versions;
  consumers must ignore unknown fields.
* Absent information is `null` (or `[]`), never guessed.

## Document

```json
{
  "feed_version": "agentready-feed/1.0",
  "generated_at": "2026-11-07T12:00:00+00:00",
  "merchant": { "id": "uuid", "name": "Sharma Electricals", "default_currency": "INR" },
  "item_count": 1,
  "items": [ /* Item */ ]
}
```

## Item

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Stable AgentReady product id |
| `version` | int | Increments on every merchant edit or applied rescan change |
| `title` | string | Product name |
| `identifiers` | `{sku, gtin, mpn}` | GTINs pass check-digit validation or are flagged |
| `brand`, `manufacturer` | string \| null | |
| `category` | `{value, inferred}` \| null | `inferred: true` when the category was not stated on the page |
| `description` | string \| null | |
| `offer.price` | `{amount: "450.00", currency: "INR"}` \| null | Decimal string; ISO 4217 currency |
| `offer.availability` | string \| null | schema.org value (`InStock`, `OutOfStock`, `PreOrder`, …) only if explicitly stated |
| `offer.url` | string | Product page (or the page where it was listed) |
| `images` | string[] | Absolute URLs |
| `attributes` | `[{key, name, value, unit}]` | Original units preserved |
| `variants` | `[{name, sku, price, attributes}]` | |
| `provenance.source_url` | string | Page the data was extracted from |
| `provenance.merchant_edited_fields` | string[] | Fields corrected by the merchant |
| `status` | `{approval, quality}` | `approval` is `approved` unless unapproved items were explicitly included |
| `updated_at` | ISO 8601 | |
