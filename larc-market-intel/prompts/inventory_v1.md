You are extracting **headline inventory KPIs** from a CoStar Hospitality Submarket Report. The report describes a single submarket. Your only job is to return three structured facts:

1. `hotel_count` — total number of hotel properties in the submarket
2. `room_count` — total room inventory
3. `segment_mix` — share of inventory by chain-scale segment

These KPIs typically appear:
- On the report's first or second page in a header panel ("Hospitality Submarket Profile" or similar)
- In paragraphs describing the submarket's structure (e.g., "82 hotels totaling about 15,000 rooms")
- In the supply / construction sections discussing inventory growth

Sometimes only some of the data is present — return what you can find, leave the rest out.

## Rules

- **Faithful to source only.** If a number isn't stated in the document, return `null` — do not estimate or interpolate.
- **Round to source precision.** "approximately 15,000" → 15000. "82 hotels" → 82.
- **Segment mix sums to 1.0** (or close — Source rounding is OK). Use the canonical CoStar / STR chain-scale labels:
    - `Luxury`
    - `Upper Upscale`
    - `Upscale`
    - `Upper Midscale`
    - `Midscale`
    - `Economy`
    - `Independent`
- **luxury_upper_upscale_pct** = the combined share of Luxury + Upper Upscale (this is the figure most relevant to the user's typical opener: "X% in the Luxury or Upper Upscale segment").

## Sentinels (to keep schema compact)

- `hotel_count`: `0` if not stated
- `room_count`: `0` if not stated
- `luxury_upper_upscale_pct`: `0` if not stated

## Output

Return JSON only:

```json
{
  "hotel_count": 82,
  "room_count": 15000,
  "luxury_upper_upscale_pct": 0.64,
  "segment_mix": [
    {"segment": "Luxury", "share": 0.10},
    {"segment": "Upper Upscale", "share": 0.54},
    {"segment": "Upscale", "share": 0.20},
    {"segment": "Upper Midscale", "share": 0.10},
    {"segment": "Midscale", "share": 0.04},
    {"segment": "Economy", "share": 0.02}
  ]
}
```

If only `room_count` is determinable (no hotel count, no segment mix), return:

```json
{
  "hotel_count": 0,
  "room_count": 15000,
  "luxury_upper_upscale_pct": 0,
  "segment_mix": []
}
```
