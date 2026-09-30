#!/usr/bin/env python3
"""
ComparePV daily panel-price monitor.

Uses the normal HTML panel pages and parses the "Price offers" table.
This avoids relying on ComparePV's .md representation, which can change
independently of the visible page.

The script records all offers found for the selected panels and keeps a
history in data/prices.csv.
"""

from __future__ import annotations

import csv
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup


TRACKED_PANELS = {
    "JA Solar JAM54D40-465/LB": {
        "url": "https://comparepv.com/panel/ja-solar-jam54d40-465-lb",
        "alert_pln": 340.0,
    },
    "JA Solar JAM54D40-465/LR": {
        "url": "https://comparepv.com/panel/ja-solar-jam54d40-465-lr",
        "alert_pln": 325.0,
    },
    "JA Solar JAM54D40-460/LB": {
        "url": "https://comparepv.com/panel/ja-solar-jam54d40-460-lb-1",
        "alert_pln": 315.0,
    },
    "JA Solar JAM54D40-460/LR": {
        "url": "https://comparepv.com/panel/ja-solar-jam54d40-460-lr",
        "alert_pln": 315.0,
    },
}

DATA_FILE = Path("data/prices.csv")
TIMEOUT = 30
REQUEST_DELAY = 0.7

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
            "Mozilla/5.0 (compatible; PVPriceMonitor/3.0; "
            "+https://github.com/blotunga-prog/pv-price-monitor)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
)


def parse_price(text: str) -> tuple[float | None, str]:
    text = " ".join(text.replace("\u00a0", " ").split())

    match = re.search(
        r"(?<!\d)(\d[\d\s\u202f]*(?:[.,]\d{1,2})?)\s*(zł|PLN|€|EUR|Kč|CZK)",
        text,
        re.I,
    )
    if not match:
        return None, ""

    raw = match.group(1).replace(" ", "").replace("\u202f", "")

    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")

    try:
        return round(float(raw), 2), match.group(2)
    except ValueError:
        return None, ""


def parse_offer_table(html: str, source_url: str, model: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    for table in soup.find_all("table"):
        headers = [
            " ".join(cell.stripped_strings).casefold()
            for cell in table.find_all("th")
        ]

        if not headers:
            continue

        required = {"shop", "country", "price", "stock"}
        if not required.issubset(set(headers)):
            continue

        header_map = {name: i for i, name in enumerate(headers)}

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue

            values = [" ".join(c.stripped_strings) for c in cells]

            # Header row
            if values and values[0].casefold() == "shop":
                continue

            def get(name: str) -> str:
                idx = header_map.get(name)
                if idx is None or idx >= len(values):
                    return ""
                return values[idx]

            shop = get("shop")
            country = get("country")
            price_text = get("price")
            stock = get("stock")
            updated = get("offer updated")

            price, currency = parse_price(price_text)

            if not shop or price is None:
                continue

            vat = ""
            lower_price = price_text.casefold()
            if "incl. vat" in lower_price:
                vat = "incl. VAT"
            elif "excl. vat" in lower_price:
                vat = "excl. VAT"

            results.append(
                {
                    "model": model,
                    "shop": shop,
                    "country": country,
                    "price": price,
                    "currency": currency,
                    "vat": vat,
                    "stock": stock,
                    "updated": updated,
                    "source_url": source_url,
                }
            )

        if results:
            break

    return results


def fetch_panel(model: str, cfg: dict) -> list[dict]:
    response = SESSION.get(cfg["url"], timeout=TIMEOUT)
    response.raise_for_status()
    return parse_offer_table(response.text, cfg["url"], model)


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
            pass

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

        threshold = TRACKED_PANELS[row["model"]]["alert_pln"]

        if (
            row["currency"].casefold() in {"zł", "pln"}
            and price <= threshold
        ):
            messages.append(
                f"ALERT: {row['model']} | {row['shop']} | "
                f"{price:.2f} zł <= {threshold:.2f} zł"
            )

    print("\n=== ALERTS ===")

    if messages:
        print("\n".join(messages))
    else:
        print("No price changes or threshold alerts.")


def main() -> int:
    checked_at = datetime.now(timezone.utc).isoformat()

    all_rows: list[dict] = []
    errors: list[str] = []

    for model, cfg in TRACKED_PANELS.items():
        try:
            rows = fetch_panel(model, cfg)
            all_rows.extend(rows)

            polish = [
                r for r in rows
                if r["currency"].casefold() in {"zł", "pln"}
                and "poland" in r["country"].casefold()
            ]

            if polish:
                cheapest = min(polish, key=lambda r: r["price"])
                print(
                    f"{model}: {len(polish)} PL offers; "
                    f"cheapest {cheapest['price']:.2f} zł "
                    f"({cheapest['shop']})"
                )
            else:
                print(f"{model}: no Polish offers.")

        except requests.RequestException as exc:
            msg = f"{model}: HTTP error: {exc}"
            print(f"ERROR: {msg}", file=sys.stderr)
            errors.append(msg)

        except Exception as exc:
            msg = f"{model}: parser error: {exc}"
            print(f"ERROR: {msg}", file=sys.stderr)
            errors.append(msg)

        time.sleep(REQUEST_DELAY)

    if not all_rows:
        print(
            "\nERROR: zero offers were parsed from ComparePV.",
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

    print(f"\nSaved {len(all_rows)} offers to {DATA_FILE}")

    if errors:
        print(f"Completed with {len(errors)} page error(s).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

