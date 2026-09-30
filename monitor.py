#!/usr/bin/env python3
"""
Daily ComparePV price monitor.

- Reads ComparePV's plain-Markdown offers pages.
- Looks for selected PV modules in Polish offers.
- Saves a historical CSV under data/prices.csv.
- Prints alerts when a tracked offer changes price or when a new/cheaper
  offer appears.

The script deliberately treats ComparePV as the source of the observed
price, not as the seller. Before buying, verify the shop's own listing.
"""

from __future__ import annotations

import csv
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE_URL = "https://comparepv.com/offers.md"

# Number of result pages to inspect. ComparePV's Polish panel market is
# currently around 1,300 offers, so 15 pages x 100 covers the current range.
MAX_PAGES = int(os.getenv("COMPAREPV_MAX_PAGES", "15"))
PAGE_SIZE = 100

# Edit this list as we add/remove panels.
TRACKED_MODELS = {
    "JA Solar JAM54D40-465/LB_BFT": [
        "JAM54D40-465/LB_BFT",
        "JAM54D40-465/LB BF",
    ],
    "JA Solar JAM54D40-465/LR_BF": [
        "JAM54D40-465/LR_BF",
        "JAM54D40-465 LR BF",
    ],
    "JA Solar JAM54D41-465/LR_FB": [
        "JAM54D41-465/LR_FB",
        "JAM54D41-465 LR FB",
    ],
    "JA Solar JAM54D41-460/LB_FB": [
        "JAM54D41-460/LB_FB",
        "JAM54D41-460 LB FB",
    ],
    "Recom 375W Full Black": [
        "Recom 375W Full Black",
        "RCM-375-6ME",
    ],
}

ALERT_PRICE_PLN = {
    "JA Solar JAM54D40-465/LB_BFT": 330.0,
    "JA Solar JAM54D40-465/LR_BF": 320.0,
    "JA Solar JAM54D41-465/LR_FB": 330.0,
    "JA Solar JAM54D41-460/LB_FB": 325.0,
    "Recom 375W Full Black": 220.0,
}

DATA_FILE = Path("data/prices.csv")
TIMEOUT = 30

CSV_FIELDS = [
    "checked_at_utc",
    "model",
    "product",
    "shop",
    "country",
    "price_pln",
    "vat",
    "stock",
    "updated",
    "source_url",
]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def fetch_page(page: int) -> str:
    params = {
        "country": "PL",
        "currency": "PLN",
        "product_type": "panel",
        "matched": "true",
        "page_size": PAGE_SIZE,
        "page": page,
    }
    url = f"{BASE_URL}?{urlencode(params)}"
    response = requests.get(
        url,
        timeout=TIMEOUT,
        headers={
            "User-Agent": "pv-price-monitor/1.0 (personal price monitoring)",
            "Accept": "text/markdown,text/plain;q=0.9,*/*;q=0.1",
        },
    )
    response.raise_for_status()
    return response.text


def parse_price(text: str) -> float | None:
    # Handles "346.28 zł", "350 zł", and thousands separators.
    match = re.search(r"(?<!\d)(\d{1,3}(?:[ .]\d{3})*(?:[,.]\d{1,2})?|\d+(?:[,.]\d{1,2})?)\s*zł", text, re.I)
    if not match:
        return None

    value = match.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    # If the source used a dot as decimal separator, undo the thousands
    # conversion for values such as 346.28.
    raw = match.group(1).replace(" ", "")
    if "." in raw and "," not in raw and len(raw.split(".")[-1]) == 2:
        value = raw
    try:
        return float(value)
    except ValueError:
        return None


def model_for(text: str) -> str | None:
    n = normalize(text)
    for model, needles in TRACKED_MODELS.items():
        if any(normalize(needle) in n for needle in needles):
            return model
    return None


def parse_offer_lines(markdown: str) -> list[dict]:
    """
    Parse the readable text returned by ComparePV's .md endpoint.

    ComparePV's plain-Markdown format can evolve. We therefore use a
    deliberately tolerant line-based parser rather than relying on HTML.
    """
    results: list[dict] = []
    lines = [re.sub(r"\s+", " ", x).strip() for x in markdown.splitlines() if x.strip()]

    for i, line in enumerate(lines):
        model = model_for(line)
        if not model:
            continue

        price = parse_price(line)
        if price is None:
            # Sometimes the price is separated from the product line.
            nearby = " ".join(lines[i : i + 3])
            price = parse_price(nearby)

        if price is None:
            continue

        # ComparePV commonly presents:
        # Product ... Panels ... Shop ... Poland ... 346.28 zł incl. VAT In stock ...
        shop = ""
        country = "PL"
        vat = ""
        stock = ""
        updated = ""

        # Country is fixed by our query, but keep the first recognizable
        # shop/country/stock information from the surrounding text.
        context = " ".join(lines[max(0, i - 1) : min(len(lines), i + 3)])

        stock_match = re.search(
            r"\b(In stock|Out of stock|Available|Unavailable)\b",
            context,
            re.I,
        )
        if stock_match:
            stock = stock_match.group(1)

        vat_match = re.search(r"\b(incl\. VAT|excl\. VAT)\b", context, re.I)
        if vat_match:
            vat = vat_match.group(1)

        updated_match = re.search(
            r"(\d+\s*(?:min|mins|h|d)\s*ago|yesterday|today)",
            context,
            re.I,
        )
        if updated_match:
            updated = updated_match.group(1)

        # Try to recover a shop name from common ComparePV row ordering.
        shop_match = re.search(
            r"\bPanels?\b\s+(?:Panel page\s+)?(.+?)\s+PL\b",
            context,
            re.I,
        )
        if shop_match:
            shop = shop_match.group(1).strip()

        results.append(
            {
                "model": model,
                "product": line,
                "shop": shop,
                "country": country,
                "price_pln": round(price, 2),
                "vat": vat,
                "stock": stock,
                "updated": updated,
                "source_url": BASE_URL,
            }
        )

    # De-duplicate rows created by overlapping context.
    unique = {}
    for row in results:
        key = (row["model"], row["product"], row["shop"], row["price_pln"])
        unique[key] = row
    return list(unique.values())


def load_previous() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    with DATA_FILE.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_history(rows: list[dict]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    exists = DATA_FILE.exists()
    with DATA_FILE.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def alert(rows: list[dict], previous: list[dict]) -> None:
    previous_latest: dict[tuple[str, str], float] = {}
    for row in previous:
        try:
            key = (row["model"], row["shop"])
            previous_latest[key] = float(row["price_pln"])
        except (KeyError, ValueError):
            continue

    messages = []
    for row in rows:
        key = (row["model"], row["shop"])
        old = previous_latest.get(key)
        new = row["price_pln"]

        if old is not None and abs(new - old) >= 0.01:
            direction = "↓" if new < old else "↑"
            pct = (new - old) / old * 100
            messages.append(
                f"{direction} {row['model']} | {row['shop'] or 'shop?'} | "
                f"{old:.2f} -> {new:.2f} zł ({pct:+.1f}%)"
            )

        threshold = ALERT_PRICE_PLN.get(row["model"])
        if threshold is not None and new <= threshold:
            messages.append(
                f"🔔 BELOW THRESHOLD: {row['model']} | "
                f"{row['shop'] or 'shop?'} | {new:.2f} zł <= {threshold:.2f} zł"
            )

    if messages:
        print("\nALERTS")
        print("\n".join(messages))


def main() -> int:
    checked_at = datetime.now(timezone.utc).isoformat()
    all_rows: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        try:
            text = fetch_page(page)
        except requests.RequestException as exc:
            print(f"ERROR: ComparePV page {page}: {exc}", file=sys.stderr)
            continue

        rows = parse_offer_lines(text)
        all_rows.extend(rows)
        print(f"page={page}: {len(rows)} tracked offers")

        # Avoid hammering the site.
        time.sleep(0.5)

    if not all_rows:
        print("ERROR: no tracked offers found; history was not modified.", file=sys.stderr)
        return 2

    for row in all_rows:
        row["checked_at_utc"] = checked_at

    previous = load_previous()
    append_history(all_rows)
    alert(all_rows, previous)

    print(f"\nSaved {len(all_rows)} offers to {DATA_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
