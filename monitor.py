import csv
import os
from pathlib import Path
import re
from datetime import datetime, timezone
from html import unescape

import requests


TRACKED_PANELS = {
    # ============================================================
    # JA SOLAR — obecnie monitorowane
    # ============================================================

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


    # ============================================================
    # AIKO NEOSTAR 3N54 — ABC, mono-glass
    # ============================================================

    "AIKO-A470-MCE54Mw": {
        "url": "https://comparepv.com/panel/aiko-aiko-a470-mce54mw",
        "alert_pln": None,
    },

    "AIKO-A475-MCE54Mw": {
        "url": "https://comparepv.com/panel/aiko-aiko-a475-mce54mw",
        "alert_pln": None,
    },

    "AIKO-A485-MCE54Mw": {
        "url": "https://comparepv.com/panel/aiko-aiko-a485-mce54mw",
        "alert_pln": None,
    },


    # ============================================================
    # TRINA — Vertex S+ / TOPCon bifacial
    # ============================================================

    "Trina Solar TSM-465NED9R.28": {
        "url": "https://comparepv.com/panel/trina-solar-tsm-465ned9r-28",
        "alert_pln": None,
    },


    # ============================================================
    # JINKO — Tiger Neo 48HL4M
    # All Black, mono-facial, dual glass
    # ============================================================

    "Jinko Solar JKM450-475N-48HL4M-DB-Z2-EN (465W)": {
        "url": (
            "https://comparepv.com/panel/"
            "jinkosolar-jkm450-475n-48hl4m-db-z2-en-465w"
        ),
        "alert_pln": None,
    },


    # ============================================================
    # LONGi — benchmark
    # Hi-MO X6 Explorer / HPBC
    # Uwaga: 1800 mm długości
    # ============================================================

    "LONGi LR7-54HTH-465M": {
        "url": "https://comparepv.com/panel/longi-lr7-54hth-465m",
        "alert_pln": None,
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

    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        text,
    )

    text = re.sub(
        r"<[^>]+>",
        "",
        text,
    )

    text = (
        text
        .replace("**", "")
        .replace("__", "")
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def parse_price(text):
    text = clean_markdown(text)

    # Prefer PLN/zł, but also accept EUR and other currencies
    # for historical monitoring.

    m = re.search(
        r"([0-9][0-9\s.,]*)\s*"
        r"(zł|PLN|EUR|€|USD|\$|GBP|£)\b",
        text,
        re.I,
    )

    if not m:
        return None, None

    raw = m.group(1).replace(" ", "")

    currency = m.group(2).upper()

    if currency == "ZŁ":
        currency = "PLN"

    elif currency == "€":
        currency = "EUR"

    elif currency == "$":
        currency = "USD"

    elif currency == "£":
        currency = "GBP"


    # Handle both:
    # 346.28
    # 346,28
    # 1.234,56
    # 1,234.56

    if "," in raw and "." in raw:

        if raw.rfind(",") > raw.rfind("."):
            raw = (
                raw
                .replace(".", "")
                .replace(",", ".")
            )

        else:
            raw = raw.replace(",", "")

    elif "," in raw:

        parts = raw.split(",")

        if len(parts[-1]) in (1, 2):
            raw = (
                "".join(parts[:-1])
                + "."
                + parts[-1]
            )

        else:
            raw = raw.replace(",", "")

    elif raw.count(".") > 1:

        parts = raw.split(".")

        raw = (
            "".join(parts[:-1])
            + "."
            + parts[-1]
        )

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

    cells = [
        c.strip()
        for c in line[1:].split("|")
    ]

    return cells


def is_separator_row(cells):
    return (
        bool(cells)
        and all(
            re.fullmatch(
                r":?-{2,}:?",
                c.replace(" ", ""),
            )
            for c in cells
        )
    )


def parse_tables(markdown):
    """
    Parse ComparePV offers table.

    We identify the table by its column structure:

    Shop | Country | Price | Per kWp |
    Stock | Offer updated | Link to shop
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

        has_shop = any(
            x == "shop"
            for x in normalized
        )

        has_country = any(
            x == "country"
            for x in normalized
        )

        has_price = any(
            x == "price"
            for x in normalized
        )

        has_stock = any(
            x == "stock"
            for x in normalized
        )

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

            shop = clean_markdown(
                cells[shop_idx]
            )

            price, currency = parse_price(
                cells[price_idx]
            )

            if not shop or price is None:
                continue

            offers.append({
                "shop": shop,
                "country": clean_markdown(
                    cells[country_idx]
                ),
                "price": price,
                "currency": currency,
                "stock": clean_markdown(
                    cells[stock_idx]
                ),
                "updated": clean_markdown(
                    cells[updated_idx]
                ),
            })

        # Stop after first actual offers table.

        if offers:
            return offers

    return offers


def fetch_panel(panel_name, source_url):

    # GitHub Actions gets HTTP 403 directly from ComparePV.
    # Jina Reader fetches the public page server-side.

    jina_url = JINA_PREFIX + source_url

    jina_api_key = os.getenv(
        "JINA_API_KEY"
    )

    headers = {}

    if jina_api_key:
        headers["Authorization"] = (
            f"Bearer {jina_api_key}"
        )

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
            "Jina returned an unexpectedly short "
            f"response ({len(text)} bytes)."
        )

    offers = parse_tables(text)

    if not offers:

        preview = " ".join(
            text[:500].split()
        )

        raise RuntimeError(
            "zero offers parsed. "
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

            rows = list(
                csv.DictReader(f)
            )

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

            previous[key] = float(
                row["price"]
            )

        except (
            ValueError,
            TypeError,
        ):

            pass

    return previous


def main():

    Path(
        CSV_PATH
    ).parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    previous = load_previous()

    checked_at = datetime.now(
        timezone.utc
    ).isoformat(
        timespec="seconds"
    )

    all_rows = []

    total_offers = 0

    successful_models = 0

    failed_models = 0


    for model, cfg in TRACKED_PANELS.items():

        try:

            offers = fetch_panel(
                model,
                cfg["url"],
            )

            successful_models += 1

            total_offers += len(
                offers
            )


            # ----------------------------------------------------
            # Save every current offer
            # ----------------------------------------------------

            for offer in offers:

                all_rows.append({
                    "checked_at_utc": checked_at,
                    "model": model,
                    "shop": offer["shop"],
                    "country": offer["country"],
                    "price": (
                        f'{offer["price"]:.2f}'
                    ),
                    "currency": offer["currency"],
                    "vat": "",
                    "stock": offer["stock"],
                    "updated": offer["updated"],
                    "source_url": cfg["url"],
                })


                # ------------------------------------------------
                # Price change detection
                # ------------------------------------------------

                old = previous.get(
                    (
                        model,
                        offer["shop"],
                        offer["currency"],
                    )
                )

                if (
                    old is not None
                    and abs(
                        old - offer["price"]
                    ) >= 0.01
                ):

                    direction = (
                        "DOWN"
                        if offer["price"] < old
                        else "UP"
                    )

                    print(
                        f"PRICE {direction}: "
                        f"{model} / "
                        f"{offer['shop']}: "
                        f"{old:.2f} -> "
                        f"{offer['price']:.2f} "
                        f"{offer['currency']}"
                    )


                # ------------------------------------------------
                # Price alert
                #
                # Existing JA Solar models have thresholds.
                # New models currently have alert_pln=None,
                # so they are monitored without generating
                # an alert on every offer.
                # ------------------------------------------------

                alert_pln = cfg.get(
                    "alert_pln"
                )

                if (
                    offer["currency"] == "PLN"
                    and alert_pln is not None
                    and offer["price"] <= alert_pln
                ):

                    print(
                        f"ALERT: "
                        f"{model} / "
                        f"{offer['shop']} = "
                        f"{offer['price']:.2f} PLN "
                        f"<= {alert_pln:.2f} PLN"
                    )


            # ----------------------------------------------------
            # Cheapest offer
            # ----------------------------------------------------

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

            failed_models += 1

            print(
                f"ERROR: {model}: {exc}"
            )


    # ------------------------------------------------------------
    # Do not destroy the existing CSV if every source fails.
    # ------------------------------------------------------------

    if not all_rows:

        print(
            "ERROR: zero offers were parsed "
            "from ComparePV through Jina Reader."
        )

        raise SystemExit(2)


    # ------------------------------------------------------------
    # Save CSV
    # ------------------------------------------------------------

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

        writer.writerows(
            all_rows
        )


    # ------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------

    print(
        f"Saved {len(all_rows)} current offers "
        f"to {CSV_PATH}."
    )

    print(
        f"Total offers parsed: "
        f"{total_offers}"
    )

    print(
        f"Models successful: "
        f"{successful_models}/"
        f"{len(TRACKED_PANELS)}"
    )

    print(
        f"Models failed: "
        f"{failed_models}"
    )


if __name__ == "__main__":
    main()
