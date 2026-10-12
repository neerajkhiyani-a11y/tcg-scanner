# tcg-scanner

eBay bargain scanner for the Trading Card Games workstation (Notion CNS).

## What it does

Reads Target rows (Scanner ✓) from Notion, searches eBay active listings for each
one, and reports any listing whose total price (item + shipping) is at or under
the row's **Buy Under**. Graded targets are filtered by grader and minimum grade,
and PSA slabs are cert-verified when a `PSA_TOKEN` is set.

The scanner never decides what a fair price is. **Buy Under** and **eBay Query**
on each Target row are the whole interface, and the backtests rewrite those.

## Architecture

```
Notion Targets ──(Claude, MCP)──▶ targets.json ──▶ scanner.py ──▶ hits.json ──(Claude, MCP)──▶ Notion Targets
                                                      │
                                              eBay Browse API
                                              PSA cert lookup
```

`scanner.py` is pure eBay. It has no Notion dependency, so it needs no Notion
token and runs anywhere Python 3.9+ does. The scheduled Claude session does the
Notion side with the connector it already has.

## Setup

1. eBay developer account → Production app → Client ID + Client Secret.
2. (Optional) PSA API token for cert verification.
3. Export as env vars where the scanner runs.

```
export EBAY_CLIENT_ID=...
export EBAY_CLIENT_SECRET=...
export PSA_TOKEN=...          # optional
```

## Run

```
python3 scanner.py < targets.json > hits.json
```

`targets.json`:

```json
[
  {
    "id": "notion-page-id",
    "name": "Snorlax (11) · Jungle PSA 8",
    "query": "Snorlax Jungle 11 holo PSA 8",
    "buy_under": 240,
    "type": "Graded",
    "grader": "PSA",
    "min_grade": 8
  }
]
```

Each hit is the input row plus `listing_url`, `listing_title`, `price`,
`shipping`, `total`, `grader_found`, `grade_found`,
`cert_number`, `cert_ok`, `found_at`.

## Filters applied

- Buy It Now / Best Offer only (no auctions)
- US sellers, USD
- Graded targets: title must carry a grader + grade; grader must match unless `Any`; grade ≥ `min_grade`
- Raw / sealed targets: slabbed listings and proxy/custom/digital/code-card titles are skipped
- Edition match: "1st edition" / "shadowless" in the query must appear in the title; "unlimited" rejects 1st Ed and Shadowless titles
- No seller data is returned or stored (keyset is exempt from eBay account-deletion notifications)
- Max 3 hits per target (`SCANNER_MAX_PER_TARGET`)

## API budget

One Browse search per target, plus one item lookup per graded hit. eBay's free
tier is 5,000 calls/day, so even 200 targets scanned four times a day is fine.

## Writing eBay Query strings

Keep them to the tokens a seller would actually type: Pokémon name, set, card
number, "holo" if relevant, and the grade for graded rows. Don't include
"Pokémon" (every listing has it) or words like "mint" or "rare" (noise).

Good: `Snorlax Jungle 11 holo PSA 8`
Bad:  `Pokemon Snorlax Jungle holo rare near mint PSA 8 graded card`
