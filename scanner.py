#!/usr/bin/env python3
"""
tcg-scanner — eBay bargain scanner for the Trading Card Games workstation.

Pure eBay side. Takes a JSON list of targets on stdin, returns a JSON list of
hits on stdout. Notion reads/writes are done by Claude in the scheduled
session, so this script needs no Notion token.

Input (stdin), one object per Target row with Scanner = true:
  {
    "id":        "<notion page id>",
    "name":      "Snorlax (11) · Jungle PSA 8",
    "query":     "Snorlax Jungle 11 holo PSA 8",   # eBay Query column
    "buy_under": 240.0,                            # Buy Under column (incl. shipping)
    "type":      "Graded" | "Raw Single" | "Sealed – ETB" | ...,
    "grader":    "PSA" | "CGC" | "BGS" | "Any" | null,
    "min_grade": 8 | null
  }

Output (stdout): list of hits, each the input target plus:
  {
    "listing_url", "listing_title", "price", "shipping", "total",
    "grader_found", "grade_found",
    "cert_number", "cert_ok", "found_at"
  }

Env:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET   required (production keys)
  PSA_TOKEN                            optional; enables PSA cert verification
  SCANNER_MAX_PER_TARGET               optional, default 3 hits per target

Exit code 0 even when no hits; stderr carries a one-line summary.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

EBAY_OAUTH = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_ITEM = "https://api.ebay.com/buy/browse/v1/item/"
PSA_CERT = "https://api.psacard.com/publicapi/cert/GetByCertNumber/"
MARKETPLACE = "EBAY_US"
POKEMON_TCG_CATEGORY = "183454"  # CCG Individual Cards; sealed uses 183456 (Sealed Packs) + 261328 (Booster Boxes)

GRADE_RE = re.compile(r"\b(PSA|CGC|BGS|SGC|ACE)\s*-?\s*(10|9\.5|9|8\.5|8|7\.5|7|6\.5|6|5|4|3|2|1)\b", re.I)
CERT_RE = re.compile(r"\b(\d{8,9})\b")


def _get(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ebay_token(client_id, client_secret):
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode()
    req = urllib.request.Request(EBAY_OAUTH, data=body, headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["access_token"]


def search(token, target):
    """One Browse API call per target. BIN / Best Offer only, US sellers, priced under the ceiling."""
    ceiling = float(target["buy_under"])
    filters = [
        "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
        f"price:[..{ceiling:.2f}]",
        "priceCurrency:USD",
        "itemLocationCountry:US",
        "conditions:{NEW|USED|UNSPECIFIED}",
    ]
    params = {
        "q": target["query"],
        "filter": ",".join(filters),
        "sort": "price",
        "limit": "50",
    }
    url = EBAY_SEARCH + "?" + urllib.parse.urlencode(params)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    }
    data = _get(url, headers)
    return data.get("itemSummaries", [])


def parse_grade(title):
    m = GRADE_RE.search(title or "")
    if not m:
        return None, None
    return m.group(1).upper(), float(m.group(2))


FIRST_ED_RE = re.compile(r"\b(1st|first)\s*(ed|edition)?\b", re.I)
SHADOWLESS_RE = re.compile(r"\bshadowless\b", re.I)


def edition_ok(query, title):
    """1st Ed / Shadowless / Unlimited are different cards at very different prices.
    The query's edition words must match the listing title."""
    q, t = query or "", title or ""
    if FIRST_ED_RE.search(q) and not FIRST_ED_RE.search(t):
        return False
    if SHADOWLESS_RE.search(q) and not SHADOWLESS_RE.search(t):
        return False
    if re.search(r"\bunlimited\b", q, re.I) and (FIRST_ED_RE.search(t) or SHADOWLESS_RE.search(t)):
        return False
    return True


def item_cert(token, item_id):
    """Fetch item aspects to find a certification number. Only called on hits."""
    try:
        data = _get(EBAY_ITEM + urllib.parse.quote(item_id, safe=""), {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        })
    except Exception:
        return None
    for a in data.get("localizedAspects", []):
        if "cert" in a.get("name", "").lower():
            m = CERT_RE.search(a.get("value", ""))
            if m:
                return m.group(1)
    m = CERT_RE.search(data.get("title", ""))
    return m.group(1) if m else None


def psa_verify(cert, grader, grade, psa_token):
    """Returns True/False when verifiable, None when not."""
    if grader != "PSA" or not cert or not psa_token:
        return None
    try:
        data = _get(PSA_CERT + cert, {"Authorization": f"bearer {psa_token}"})
    except Exception:
        return None
    psa = data.get("PSACert") or {}
    if not psa:
        return False
    try:
        return float(psa.get("CardGrade", "").split()[-1]) == float(grade)
    except Exception:
        return False


def total_price(item):
    price = float(item.get("price", {}).get("value", 0))
    ship = 0.0
    for opt in item.get("shippingOptions", []) or []:
        cost = opt.get("shippingCost", {})
        if cost.get("value") is not None:
            ship = float(cost["value"])
            break
    return price, ship, round(price + ship, 2)


def scan(targets, token, psa_token, max_hits):
    hits, calls = [], 0
    for t in targets:
        if not t.get("query") or not t.get("buy_under"):
            continue
        try:
            items = search(token, t)
            calls += 1
        except Exception as e:
            print(f"search failed for {t.get('name')}: {e}", file=sys.stderr)
            continue
        found = 0
        for it in items:
            title = it.get("title", "")
            price, ship, total = total_price(it)
            if total > float(t["buy_under"]):
                continue
            if not edition_ok(t["query"], title):
                continue
            grader_found, grade_found = parse_grade(title)
            want_graded = (t.get("type") == "Graded")
            if want_graded:
                if grade_found is None:
                    continue
                if t.get("grader") and t["grader"] != "Any" and grader_found != t["grader"]:
                    continue
                if t.get("min_grade") and grade_found < float(t["min_grade"]):
                    continue
            else:
                # Raw / sealed target: skip slabbed listings and obvious junk
                if grade_found is not None:
                    continue
                if re.search(r"\b(proxy|custom|replica|digital|code card|reprint)\b", title, re.I):
                    continue
            cert = item_cert(token, it["itemId"]) if want_graded else None
            if want_graded:
                calls += 1
            cert_ok = psa_verify(cert, grader_found, grade_found, psa_token)
            # No seller username/feedback: the eBay keyset is exempt from
            # account-deletion notifications on the basis that we store no eBay user data.
            hits.append({
                **t,
                "listing_url": it.get("itemWebUrl"),
                "listing_title": title,
                "price": price,
                "shipping": ship,
                "total": total,
                "grader_found": grader_found,
                "grade_found": grade_found,
                "cert_number": cert,
                "cert_ok": cert_ok,
                "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            found += 1
            if found >= max_hits:
                break
        time.sleep(0.2)
    return hits, calls


def main():
    cid, sec = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not sec:
        print("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set", file=sys.stderr)
        sys.exit(2)
    targets = json.load(sys.stdin)
    token = ebay_token(cid, sec)
    hits, calls = scan(
        targets, token,
        os.environ.get("PSA_TOKEN"),
        int(os.environ.get("SCANNER_MAX_PER_TARGET", "3")),
    )
    print(f"{len(targets)} targets, {calls} API calls, {len(hits)} hits", file=sys.stderr)
    json.dump(hits, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
