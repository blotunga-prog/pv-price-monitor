#!/usr/bin/env python3
"""
Daily ComparePV price monitor.

This version reads the Markdown representation of the individual ComparePV
panel pages instead of trying to scrape the whole offers table. That makes
the matching much more reliable: each URL represents one exact panel family.

It:
- checks selected panel pages,
- records Polish offers (and other countries if present),
- saves history to data/prices.csv,
- reports price changes,
- reports prices below configured thresholds,
- does NOT fail just because a selected panel currently has no offers.
"""

from __future__ import annotations

import csv
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Panels to monitor
# ---------------------------------------------------------------------------
# The ComparePV panel page is the important part. The offer names on shops
# may contain suffixes such as _BFT / _BF / _FB, while ComparePV groups them
# under the corresponding catalogue panel page.

TRACKED_PANELS = {
    "JA Solar JAM54D40-465/LB": {
        "url": "https://comparepv.com/panel/ja-solar-jam54d40-465-lb.md",
        "alert_pln": 340.0,
    },
    "JA Solar JAM54D40-465/LR": {
        "url": "https://comparepv.com/panel/ja-solar-jam54d40-465-lr.md",
        "alert_pln": 325.0,
    },
    "JA Solar JAM54D40-460/LB": {
        "url": "https://comparepv.com/panel/ja-solar-jam54d40-460-lb-1.md",
        "alert_pln": 315.0,
    },
    "JA Solar JAM54D40-460/LR": {
        "url": "https://comparepv.com/panel/ja-solar-jam54d40-460-lr.md",
        "alert_pln": 315.0,
    },
}

DATA_FILE = Path("data/prices.csv")
TIMEOUT = 30
REQUEST_DELAY = 0.5

CSV_FIELDS = [
    "checked_at_utc",
    "model",
    "shop",
    "country",
    "price",
    "currency",
    "vat",
    "stock",
    "updated",
    "source_url",
]

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "pv-price-monitor/2.0 "
            "(personal daily PV price monitoring)"
        ),
        "Accept": "text/markdown,text/plain;q=0.9,*/*;q=0.1",
    }
)


def clean(text: str) -> str:
    """Collapse whitespace and Markdown table noise."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u00a0", " ")
    text = text.replace("🇵🇱", "Poland")
    text = text.replace("🇩🇪", "Germany")
    text = text.replace("🇨🇿", "Czechia")
    text = text.replace("🇮🇹", "Italy")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" |")


def parse_price_cell(cell: str) -> tuple[float | None, str]:
    """
    Parse e.g.
      346 zł incl. VAT
      2 493 Kč incl. VAT
      76.62 € incl. VAT
    """
    s = clean(cell)
    m = re.search(
        r"(?<!\d)(\d[\d\s\u202f]*(?:[.,]\d{1,2})?)\s*(zł|PLN|€|EUR|Kč|CZK)",
        s,
        re.I,
    )
    if not m:
        return None, ""

    raw = m.group(1).replace(" ", "").replace("\u202f", "")
    # European decimal comma.
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        # Keep a dot as decimal separator.
        pass

    try:
        return float(raw), m.group(2)
    except ValueError:
        return None, ""


def parse_markdown_table(markdown: str, source_url: str) -> list[dict]:
    """
    Extract rows from ComparePV's:
      Shop | Country | Price | Per kWp | Stock | Offer updated | Link to shop
    table.
    """
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]

    header_index = None
    for i, line in enumerate(lines):
        n = clean(line).casefold()
        if (
            "shop" in n
            and "country" in n
            and "price" in n
            and "stock" in n
            and "offer updated" in n
        ):
            header_index = i
            break

    if header_index is None:
        return []

    rows: list[dict] = []

    for line in lines[header_index + 1 :]:
        if not line.startswith("|"):
            # The table normally ends before the next heading/paragraph.
            if rows and line.startswith("#"):
                break
            continue

        cells = [clean(x) for x in line.strip().strip("|").split("|")]

        # Separator row: |---|---|---|
        if cells and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
            continue

        if len(cells) < 6:
            continue

        shop = cells[0]
        country = cells[1]
        price, currency = parse_price_cell(cells[2])
        stock = cells[4]
        updated = cells[5]

        if not shop or price is None:
            continue

        rows.append(
            {
                "shop": shop,
                "country": country,
                "price": round(price, 2),
                "currency": currency,
                "vat": (
                    "incl. VAT"
                    if "incl. vat" in cells[2].casefold()
                    else "excl. VAT"
                    if "excl. vat" in cells[2].casefold()
                    else ""
                ),
                "stock": stock,
                "updated": updated,
                "source_url": source_url.removesuffix(".md"),
            }
        )

    return rows


def fetch_panel(model: str, cfg: dict) -> list[dict]:
    url = cfg["url"]
    response = SESSION.get(url, timeout=TIMEOUT)
    response.raise_for_status()

    rows = parse_markdown_table(response.text, url)
    for row in rows:
        row["model"] = model
    return rows


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


def latest_prices(previous: list[dict]) -> dict[tuple[str, str, str], float]:
    result: dict[tuple[str, str, str], float] = {}

    for row in previous:
        try:
            key = (row["model"], row["shop"], row["currency"])
            result[key] = float(row["price"])
        except (KeyError, TypeError, ValueError):
            continue

    return result


def print_alerts(rows: list[dict], previous: list[dict]) -> None:
    old = latest_prices(previous)
    messages: list[str] = []

    for row in rows:
        key = (row["model"], row["shop"], row["currency"])
        price = float(row["price"])
        old_price = old.get(key)

        if old_price is not None and abs(price - old_price) >= 0.01:
            pct = (price - old_price) / old_price * 100
            direction = "DOWN" if price < old_price else "UP"
            messages.append(
                f"{direction}: {row['model']} | {row['shop']} | "
                f"{old_price:.2f} -> {price:.2f} {row['currency']} "
                f"({pct:+.1f}%)"
            )

        # Thresholds are meaningful for PLN offers only.
        threshold = TRACKED_PANELS[row["model"]].get("alert_pln")
        if (
            threshold is not None
            and row["currency"].casefold() in {"zł", "pln"}
            and price <= threshold
        ):
            messages.append(
                f"ALERT: {row['model']} | {row['shop']} | "
                f"{price:.2f} zł <= {threshold:.2f} zł"
            )

    if messages:
        print("\n=== ALERTS ===")
        for message in messages:
            print(message)
    else:
        print("\nNo price-change/threshold alerts.")


def main() -> int:
    checked_at = datetime.now(timezone.utc).isoformat()

    all_rows: list[dict] = []
    errors: list[str] = []

    for model, cfg in TRACKED_PANELS.items():
        try:
            rows = fetch_panel(model, cfg)
            all_rows.extend(rows)

            pl_rows = [
                r for r in rows
                if r["currency"].casefold() in {"zł", "pln"}
            ]
            if pl_rows:
                cheapest = min(pl_rows, key=lambda r: r["price"])
                print(
                    f"{model}: {len(pl_rows)} Polish offers; "
                    f"cheapest {cheapest['price']:.2f} zł "
                    f"({cheapest['shop']})"
                )
            else:
                print(f"{model}: no Polish offers currently.")

        except requests.RequestException as exc:
            message = f"{model}: request failed: {exc}"
            print(f"ERROR: {message}", file=sys.stderr)
            errors.append(message)
        except Exception as exc:
            message = f"{model}: parser failed: {exc}"
            print(f"ERROR: {message}", file=sys.stderr)
            errors.append(message)

        time.sleep(REQUEST_DELAY)

    # A complete lack of rows can be caused by a site/API change.
    # Treat that as a failure so GitHub Actions clearly shows a problem.
    if not all_rows:
        print(
            "ERROR: no offers were parsed from any tracked panel page.",
            file=sys.stderr,
        )
        if errors:
            print("\n".join(errors), file=sys.stderr)
        return 2

    for row in all_rows:
        row["checked_at_utc"] = checked_at

    previous = load_previous()
    append_history(all_rows)
    print_alerts(all_rows, previous)

    print(f"\nSaved {len(all_rows)} offer rows to {DATA_FILE}")

    if errors:
        print(
            f"Completed with {len(errors)} panel/page error(s). "
            "Successful rows were saved."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

