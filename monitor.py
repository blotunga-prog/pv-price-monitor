import csv
import re
from datetime import datetime, timezone
from html import unescape

import requests

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

CSV_PATH = "data/prices.csv"
JINA_PREFIX = "https://r.jina.ai/"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "PV-Price-Monitor/1.0",
    "Accept": "text/plain, text/markdown;q=0.9, */*;q=0.8",
})


def clean_markdown(text):
    text = unescape(text.strip())
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_price(text):
    text = clean_markdown(text)
    # Prefer PLN/zł, but also accept EUR and other currencies for history.
    m = re.search(r"([0-9][0-9\s.,]*)\s*(zł|PLN|EUR|€|USD|\$|GBP|£)\b", text, re.I)
    if not m:
        return None, None

    raw = m.group(1).replace(" ", "")
    currency = m.group(2).upper()
    if currency == "ZŁ":
        currency = "PLN"
    elif currency in ("€",):
        currency = "EUR"
    elif currency in ("$",):
        currency = "USD"
    elif currency in ("£",):
        currency = "GBP"

    # ComparePV prices are normally decimal numbers. Handle both 346.28 and 346,28.
    if "," in raw and "." in raw:
        # Whichever separator occurs last is treated as the decimal separator.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        parts = raw.split(",")
        raw = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) in (1, 2) else raw.replace(",", "")
    elif raw.count(".") > 1:
        parts = raw.split(".")
        raw = "".join(parts[:-1]) + "." + parts[-1]

    try:
        return float(raw), currency
    except ValueError:
        return None, None


def split_md_row(line):
    line = line.strip()
    if not line.startswith("|"):
        return []
    if line.endswith("|"):
        line = line[:-1]
    cells = [c.strip() for c in line[1:].split("|")]
    return cells


def is_separator_row(cells):
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells)


def parse_tables(markdown):
    offers = []
    lines = markdown.splitlines()

    for i in range(len(lines) - 1):
        header = split_md_row(lines[i])
        if len(header) < 4 or not is_separator_row(split_md_row(lines[i + 1])):
            continue

        normalized = [clean_markdown(x).lower() for x in header]
        price_idx = next((j for j, x in enumerate(normalized) if "price" in x), None)
        shop_idx = next((j for j, x in enumerate(normalized) if "shop" in x), None)
        country_idx = next((j for j, x in enumerate(normalized) if "country" in x), None)
        stock_idx = next((j for j, x in enumerate(normalized) if "stock" in x), None)
        updated_idx = next((j for j, x in enumerate(normalized) if "updated" in x), None)

        if price_idx is None or shop_idx is None:
            continue

        for line in lines[i + 2:]:
            cells = split_md_row(line)
            if not cells:
                break
            if len(cells) != len(header):
                break

            shop = clean_markdown(cells[shop_idx])
            price, currency = parse_price(cells[price_idx])
            if not shop or price is None:
                continue

            country = clean_markdown(cells[country_idx]) if country_idx is not None else ""
            stock = clean_markdown(cells[stock_idx]) if stock_idx is not None else ""
            updated = clean_markdown(cells[updated_idx]) if updated_idx is not None else ""

            offers.append({
                "shop": shop,
                "country": country,
                "price": price,
                "currency": currency,
                "stock": stock,
                "updated": updated,
            })

    return offers


def fetch_panel(panel_name, source_url):
    # GitHub Actions gets HTTP 403 directly from ComparePV. Jina Reader fetches
    # the same public page server-side and returns readable Markdown.
    jina_url = JINA_PREFIX + source_url

    response = SESSION.get(jina_url, timeout=30)
    response.raise_for_status()

    text = response.text
    if len(text) < 100:
        raise RuntimeError(f"Jina returned an unexpectedly short response ({len(text)} bytes).")

    offers = parse_tables(text)

    if not offers:
        preview = " ".join(text[:500].split())
        raise RuntimeError(
            f"zero offers parsed. Jina response length={len(text)}. "
            f"Preview: {preview}"
        )

    return offers


def load_previous():
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return {}

    previous = {}
    for row in rows:
        key = (row.get("model", ""), row.get("shop", ""), row.get("currency", ""))
        try:
            previous[key] = float(row["price"])
        except (ValueError, TypeError):
            pass
    return previous


def main():
    previous = load_previous()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    all_rows = []
    total_offers = 0

    for model, cfg in TRACKED_PANELS.items():
        try:
            offers = fetch_panel(model, cfg["url"])
            total_offers += len(offers)

            # Keep every current offer. This gives us a useful price history.
            for offer in offers:
                all_rows.append({
                    "checked_at_utc": checked_at,
                    "model": model,
                    "shop": offer["shop"],
                    "country": offer["country"],
                    "price": f'{offer["price"]:.2f}',
                    "currency": offer["currency"],
                    "vat": "",
                    "stock": offer["stock"],
                    "updated": offer["updated"],
                    "source_url": cfg["url"],
                })

                old = previous.get((model, offer["shop"], offer["currency"]))
                if old is not None and abs(old - offer["price"]) >= 0.01:
                    direction = "DOWN" if offer["price"] < old else "UP"
                    print(
                        f"PRICE {direction}: {model} / {offer['shop']}: "
                        f"{old:.2f} -> {offer['price']:.2f} {offer['currency']}"
                    )

                if offer["currency"] == "PLN" and offer["price"] <= cfg["alert_pln"]:
                    print(
                        f"ALERT: {model} / {offer['shop']} = "
                        f"{offer['price']:.2f} PLN <= {cfg['alert_pln']:.2f} PLN"
                    )

            cheapest = min(offers, key=lambda x: x["price"])
            print(
                f"OK: {model}: {len(offers)} offers; "
                f"cheapest {cheapest['price']:.2f} {cheapest['currency']} "
                f"at {cheapest['shop']}"
            )

        except Exception as exc:
            print(f"ERROR: {model}: {exc}")

    if not all_rows:
        print("ERROR: zero offers were parsed from ComparePV through Jina Reader.")
        raise SystemExit(2)

    fieldnames = [
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

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Saved {len(all_rows)} current offers to {CSV_PATH}.")
    print(f"Total offers parsed: {total_offers}")


if __name__ == "__main__":
    main()

