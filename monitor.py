import csv
import os
from pathlib import Path
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
    m = re.search(
        r"([0-9][0-9\s.,]*)\s*(zł|PLN|EUR|€|USD|\$|GBP|£)\b",
        text,
        re.I,
    )

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

    # ComparePV prices are normally decimal numbers.
    # Handle both 346.28 and 346,28.
    if "," in raw and "." in raw:
        # Whichever separator occurs last is treated as the decimal separator.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")

    elif "," in raw:
        parts = raw.split(",")
        raw = (
            "".join(parts[:-1]) + "." + parts[-1]
            if len(parts[-1]) in (1, 2)
            else raw.replace(",", "")
        )

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
    return bool(cells) and all(
        re.fullmatch(r":?-{2,}:?", c.replace(" ", ""))
        for c in cells
    )


def parse_tables(markdown):
    """Parse the ComparePV offers table by its exact column structure.

    Jina can return the page with headings flattened or reformatted.
    Therefore we do not depend on a '## Price offers' heading being present.
    Instead we look for the distinctive ComparePV offers header:

    Shop | Country | Price | Per kWp | Stock | Offer updated | Link to shop
    """

    offers = []
    lines = markdown.splitlines()

    for i, line in enumerate(lines):
        header = split_md_row(line)

        if len(header) < 6:
            continue

        normalized = [
            clean_markdown(x).lower()
            for x in header
        ]

        # Exact/near-exact identification of the ComparePV offers table.
        has_shop = any(x == "shop" for x in normalized)
        has_country = any(x == "country" for x in normalized)
        has_price = any(x == "price" for x in normalized)
        has_stock = any(x == "stock" for x in normalized)
        has_updated = any(
            "offer updated" in x
            for x in normalized
        )

        if not (
            has_shop
            and has_country
            and has_price
            and has_stock
            and has_updated
        ):
            continue

        separator = (
            split_md_row(lines[i + 1])
            if i + 1 < len(lines)
            else []
        )

        if not is_separator_row(separator):
            continue

        price_idx = normalized.index("price")
        shop_idx = normalized.index("shop")
        country_idx = normalized.index("country")
        stock_idx = normalized.index("stock")

        updated_idx = next(
            j
            for j, x in enumerate(normalized)
            if "offer updated" in x
        )

        for row_line in lines[i + 2:]:
            cells = split_md_row(row_line)

            if not cells:
                break

            if len(cells) != len(header):
                break

            shop = clean_markdown(cells[shop_idx])
            price, currency = parse_price(cells[price_idx])

            if not shop or price is None:
                continue

            offers.append({
                "shop": shop,
                "country": clean_markdown(cells[country_idx]),
                "price": price,
                "currency": currency,
                "stock": clean_markdown(cells[stock_idx]),
                "updated": clean_markdown(cells[updated_idx]),
            })

        # Stop after the first actual offers table.
        if offers:
            return offers

    return offers


def fetch_panel(panel_name, source_url):
    # GitHub Actions gets HTTP 403 directly from ComparePV.
    # Jina Reader fetches the same public page server-side
    # and returns readable Markdown.
    jina_url = JINA_PREFIX + source_url

    # Optional Jina API key.
    # When JINA_API_KEY exists in GitHub Actions, use it.
    # When it does not exist, keep the previous unauthenticated behaviour.
    jina_api_key = os.getenv("JINA_API_KEY")

    headers = {}

    if jina_api_key:
        headers["Authorization"] = f"Bearer {jina_api_key}"

    response = SESSION.get(
        jina_url,
        headers=headers,
        timeout=30,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Jina Reader returned HTTP 401 Unauthorized. "
            "Check JINA_API_KEY in GitHub Actions secrets."
        )

    response.raise_for_status()

    text = response.text

    if len(text) < 100:
        raise RuntimeError(
            f"Jina returned an unexpectedly short response "
            f"({len(text)} bytes)."
        )

    offers = parse_tables(text)

    if not offers:
        preview = " ".join(text[:500].split())

        raise RuntimeError(
            f"zero offers parsed. "
            f"Jina response length={len(text)}. "
            f"Preview: {preview}"
        )

    return offers


def load_previous():
    try:
        with open(
            CSV_PATH,
            newline="",
            encoding="utf-8",
        ) as f:
            rows = list(csv.DictReader(f))

    except FileNotFoundError:
        return {}

    previous = {}

    for row in rows:
        key = (
            row.get("model", ""),
            row.get("shop", ""),
            row.get("currency", ""),
        )

        try:
            previous[key] = float(row["price"])
        except (ValueError, TypeError):
            pass

    return previous


def main():
    Path(CSV_PATH).parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    previous = load_previous()

    checked_at = datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")

    all_rows = []
    total_offers = 0

    for model, cfg in TRACKED_PANELS.items():
        try:
            offers = fetch_panel(
                model,
                cfg["url"],
            )

            total_offers += len(offers)

            # Keep every current offer.
            # This gives us a useful price history.
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

                old = previous.get(
                    (
                        model,
                        offer["shop"],
                        offer["currency"],
                    )
                )

                if (
                    old is not None
                    and abs(old - offer["price"]) >= 0.01
                ):
                    direction = (
                        "DOWN"
                        if offer["price"] < old
                        else "UP"
                    )

                    print(
                        f"PRICE {direction}: "
                        f"{model} / {offer['shop']}: "
                        f"{old:.2f} -> "
                        f"{offer['price']:.2f} "
                        f"{offer['currency']}"
                    )

                if (
                    offer["currency"] == "PLN"
                    and offer["price"] <= cfg["alert_pln"]
                ):
                    print(
                        f"ALERT: "
                        f"{model} / {offer['shop']} = "
                        f"{offer['price']:.2f} PLN "
                        f"<= {cfg['alert_pln']:.2f} PLN"
                    )

            cheapest = min(
                offers,
                key=lambda x: x["price"],
            )

            print(
                f"OK: {model}: "
                f"{len(offers)} offers; "
                f"cheapest "
                f"{cheapest['price']:.2f} "
                f"{cheapest['currency']} "
                f"at {cheapest['shop']}"
            )

        except Exception as exc:
            print(
                f"ERROR: {model}: {exc}"
            )

    if not all_rows:
        print(
            "ERROR: zero offers were parsed "
            "from ComparePV through Jina Reader."
        )

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

    with open(
        CSV_PATH,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(all_rows)

    print(
        f"Saved {len(all_rows)} current offers "
        f"to {CSV_PATH}."
    )

    print(
        f"Total offers parsed: {total_offers}"
    )


if __name__ == "__main__":
    main()
