# Canonical Field Reference

This table is the source of truth for cross-provider comparability. Every canonical field is documented with its units, its LARC source, and its CoStar source. If the providers report the concept differently, the transform logic is noted here.

## Fact Table: forecast_periods

| Canonical field | Unit | LARC source | CoStar source | Notes |
|---|---|---|---|---|
| supply | room nights | HotelBIS `Supply` col; PDF "Lodging Supply Growth" table | `Available Room Nights` col | Both providers report available room-nights in the period. |
| supply_growth_pct | decimal | Computed YoY from HotelBIS; PDF table gives directly | `Supply YoY` col or computed | LARC stores 0.013 for 1.3%; CoStar may store 1.3 — transform in extractor. |
| demand | room nights | HotelBIS `Demand` | `Occupied Room Nights` | |
| occupancy | decimal (0–1) | HotelBIS `Occupancy` | `Occupancy Rate` | Divide by 100 for CoStar. |
| adr | USD | HotelBIS `ADR` | `ADR` | |
| revpar | USD | HotelBIS `RevPAR` | `RevPAR` | Sanity check: ADR × Occupancy = RevPAR ± 2%. |
| revenues | USD | HotelBIS `Revenues` | `Total Revenue` | |
| wage_growth_pct | decimal | HotelBIS `Wage Growth` | Not provided | LARC-only metric. |
| property_tax_growth_pct | decimal | HotelBIS `Property Tax Growth` | Not provided | LARC-only. |
| expense_growth_pct | decimal | PDF "Denver Expense Growth Forecast" table | Not directly | |
| hotel_ebitda | USD | HotelBIS `Hotel EBITDA` | CoStar Investment Analytics feed | CoStar publishes EBITDA in a separate product; may be absent. |
| hotel_ebitda_margin | decimal | HotelBIS `Hotel EBITDA Margin` | Computed or absent | |
| hotel_ebitda_margin_change_bps | bps | PDF "EBITDA Margin Chg" col | — | |
| cap_rate | decimal | HotelBIS `Cap Rate` | `Cap Rate` (when available) | |
| cap_rate_change_bps | bps | PDF "Cap Rate Chg (bps)" col | — | |
| hotel_value_index_2019 | index (2019=100) | HotelBIS `Hotel Value (Indexed to 2019)` | Not directly | LARC-specific index. |
| hotel_value_change_pct_from_2025 | decimal | PDF "Value Change (from 2025 Base)" | — | |

## Narrative sections

The canonical `narrative.section` enum maps provider headings as follows. When the provider uses a heading not listed, the extractor falls back to `"other"` and the raw heading is preserved in `subsection`.

| Canonical section | LARC heading | CoStar heading |
|---|---|---|
| executive_summary | "Denver Summary" | "Market Overview" |
| national_backdrop | "National Forecast Backdrop" | n/a |
| operating_results_review | "Operating Results Review" | "Performance Trends" |
| msa_economic_summary | "MSA Economic Summary" | "Economic Indicators" |
| major_local_real_estate_developments | "Major Local Real Estate Developments" | part of "Market Overview" |
| lodging_demand_drivers | "Lodging Demand Drivers" | "Demand Drivers" |
| convention_center | "Convention Center" (subsection) | n/a or "Group Demand" |
| air_traffic | "Air Traffic" (subsection) | "Airport Activity" |
| office_market | "Office Market" (subsection) | "Office Market" (when included) |
| lodging_supply | "Lodging Supply" | "Supply & Demand" |
| home_sharing_supply | "Home Sharing Supply" | "Short-Term Rental" |
| home_sharing_regulation | "Home Sharing Regulation Update" | rarely present |
| recent_transactions | "Recent Transactions" | "Sales Comparables" |
| revenue_forecast_models | "Revenue Forecast Models" | absent (CoStar's model is proprietary) |
| ebitda_forecast | "EBITDA Forecast" | absent |
| property_taxes | "Property Taxes" (subsection) | n/a |
| labor_costs | "Labor Costs" (subsection) | n/a |
| expense_forecast | "Expense Growth Forecast" | n/a |
| investment_forecast_model | "Investment Forecast Model" | "Investment Outlook" |
| cap_rate_spreads | "Denver Cap Rate Spreads" | n/a |
| investment_value_change | "Investment Value Change" | "Value Forecast" |
| forecast_revision_summary | "Forecast Revision Summary" | rarely present |

## Key normalization rules

1. **Market names**: always resolved to `markets.canonical_name` via the `market_aliases` table. `"Denver, CO"` → `"Denver"`. CoStar's "Denver, CO" and LARC's "Denver" both land in the same row.

2. **Percentages**: stored as decimals (0.013 for 1.3%). CoStar's raw values are divided by 100 if > 1 in the extractor.

3. **Dollar values**: stored as raw USD. `Price (000)` columns are multiplied by 1000 in the extractor.

4. **Dates**: stored as ISO. `"Dec-25"` parses to `2025-12-01`; `"1Q-2026"` parses to `2026-01-01`.

5. **NULL vs zero**: absent data is NULL, never 0. A hotel with 0 rooms is invalid; a hotel with unknown room count is NULL.

## When providers disagree

Disagreement is data. The app presents both values side-by-side rather than reconciling. A "reconciliation note" narrative entry can be added manually to markets where significant differences persist — surfaced in the app with a badge.
