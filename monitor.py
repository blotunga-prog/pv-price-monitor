
import csv
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from html import unescape
from urllib.parse import quote, urlparse, parse_qs

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
    "Trina Solar TSM-465NEG9R.28": {
        "url": (
            "https://comparepv.com/offers"
            "?country=PL&q=TSM-465NEG9R.28"
        ),
        "alert_pln": None,
    },
    "Jinko Solar JKM450-475N-48HL4M-DB-Z2-EN (465W)": {
        "url": (
            "https://comparepv.com/panel/"
            "jinkosolar-jkm450-475n-48hl4m-db-z2-en-465w"
        ),
        "alert_pln": None,
    },
    "LONGi LR7-54HTH-465M": {
        "url": "https://comparepv.com/panel/longi-lr7-54hth-465m",
        "alert_pln": None,
    },
}


CSV_PATH = Path("data/prices.csv")
HISTORY_PATH = Path("data/price_history.csv")
JINA_PREFIX = "https://r.jina.ai/"

FIELDNAMES = [
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


def normalize_model(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def is_poland(country):
    """Accept only offers explicitly identified as Polish."""
    value = clean_markdown(str(country)).strip().lower()

    if value in {"pl", "pol", "poland", "polska"}:
        return True

    return (
        "poland" in value
        or "polska" in value
        or "🇵🇱" in value
    )


def parse_price(text):
    text = clean_markdown(text)

    match = re.search(
        r"([0-9][0-9\s.,]*)\s*(zł|PLN|EUR|€|USD|\$|GBP|£)\b",
        text,
        re.I,
    )

    if not match:
        return None, None

    raw = match.group(1).replace(" ", "")
    currency = match.group(2).upper()

    currency = {
        "ZŁ": "PLN",
        "€": "EUR",
        "$": "USD",
        "£": "GBP",
    }.get(currency, currency)

    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts[-1]) in (1, 2):
            raw = "".join(parts[:-1]) + "." + parts[-1]
        else:
            raw = raw.replace(",", "")
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

    return [cell.strip() for cell in line[1:].split("|")]


def is_separator_row(cells):
    return (
        bool(cells)
        and all(
            re.fullmatch(r":?-{2,}:?", cell.replace(" ", ""))
            for cell in cells
        )
    )


def parse_tables(markdown, source_url):
    offers = []

    query = parse_qs(urlparse(source_url).query)
    country_filter_applied = (
        query.get("country", [""])[0].upper() == "PL"
    )
    search_term = query.get("q", [""])[0]
    normalized_search = normalize_model(search_term)

    lines = markdown.splitlines()

    for i, line in enumerate(lines):
        header = split_md_row(line)

        if len(header) < 5:
            continue

        normalized = [
            clean_markdown(cell).lower()
            for cell in header
        ]

        required = {"shop", "price", "stock"}

        if not required.issubset(set(normalized)):
            continue

        if not any("offer updated" in cell for cell in normalized):
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
        stock_idx = normalized.index("stock")
        updated_idx = next(
            j for j, cell in enumerate(normalized)
            if "offer updated" in cell
        )

        country_idx = (
            normalized.index("country")
            if "country" in normalized
            else None
        )

        product_idx = (
            normalized.index("product")
            if "product" in normalized
            else None
        )

        # Without a country column, accept data only from a URL
        # explicitly filtered to Poland.
        if country_idx is None and not country_filter_applied:
            continue

        for row_line in lines[i + 2:]:
            cells = split_md_row(row_line)

            if not cells or len(cells) != len(header):
                break

            shop = clean_markdown(cells[shop_idx])
            price, currency = parse_price(cells[price_idx])

            if not shop or price is None:
                continue

            if country_idx is not None:
                country = clean_markdown(cells[country_idx])

                if not is_poland(country):
                    continue
            else:
                country = "Poland"

            # For a search page, verify the product name when
            # ComparePV supplies a Product column.
            if normalized_search and product_idx is not None:
                product = clean_markdown(cells[product_idx])

                if normalized_search not in normalize_model(product):
                    continue

            offers.append({
                "shop": shop,
                "country": country,
                "price": price,
                "currency": currency,
                "stock": clean_markdown(cells[stock_idx]),
                "updated": clean_markdown(cells[updated_idx]),
            })

        if offers:
            return offers

    return offers


def fetch_panel(panel_name, source_url):
    # Encode the target URL so its query parameters are sent
    # to ComparePV rather than interpreted as Jina parameters.
    jina_url = JINA_PREFIX + source_url

    api_key = os.getenv("JINA_API_KEY")
    headers = {}

    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = SESSION.get(
        jina_url,
        headers=headers,
        timeout=45,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Jina Reader HTTP 401. Check JINA_API_KEY "
            "in GitHub Actions secrets."
        )

    response.raise_for_status()
    text = response.text

    if len(text) < 100:
        raise RuntimeError(
            f"Jina returned an unexpectedly short response: "
            f"{len(text)} bytes."
        )

    offers = parse_tables(text, source_url)

    if not offers:
        raise RuntimeError(
            f"No Polish offers table parsed. "
            f"Response length={len(text)}."
        )

    # Final safety check before any offer leaves this function.
    offers = [
        offer for offer in offers
        if is_poland(offer["country"])
    ]

    if not offers:
        raise RuntimeError("No offers explicitly identified as Polish.")

    return offers


def load_csv(path):
    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        if reader.fieldnames != FIELDNAMES:
            raise RuntimeError(
                f"Unexpected CSV header in {path}. "
                "The file was not modified."
            )

        return list(reader)


def load_previous_prices():
    rows = load_csv(CSV_PATH)
    previous = {}

    for row in rows:
        if not is_poland(row.get("country", "")):
            continue

        key = (
            row.get("model", ""),
            row.get("shop", ""),
            row.get("currency", ""),
        )

        try:
            previous[key] = float(row["price"])
        except (ValueError, TypeError, KeyError):
            pass

    return previous


def write_csv_temp(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )

    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)

        return Path(temp_name)

    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def main():
    try:
        previous = load_previous_prices()
    except Exception as exc:
        print(f"ERROR: could not read current prices: {exc}")
        raise SystemExit(2)

    checked_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    current_rows = []
    total_offers = 0
    successful_models = 0
    failed_models = 0

    for model, cfg in TRACKED_PANELS.items():
        try:
            offers = fetch_panel(model, cfg["url"])

            if not offers:
                raise RuntimeError("No Polish offers returned.")

            successful_models += 1
            total_offers += len(offers)

            for offer in offers:
                # Defence in depth: never write a non-Polish row.
                if not is_poland(offer.get("country", "")):
                    print(
                        f"SKIP non-Polish offer: {model} / "
                        f"{offer.get('shop', '')}"
                    )
                    continue

                row = {
                    "checked_at_utc": checked_at,
                    "model": model,
                    "shop": offer["shop"],
                    "country": "Poland",
                    "price": f'{offer["price"]:.2f}',
                    "currency": offer["currency"],
                    "vat": "",
                    "stock": offer["stock"],
                    "updated": offer["updated"],
                    "source_url": cfg["url"],
                }

                current_rows.append(row)

                key = (model, offer["shop"], offer["currency"])
                old_price = previous.get(key)

                if (
                    old_price is not None
                    and abs(old_price - offer["price"]) >= 0.01
                ):
                    direction = (
                        "DOWN" if offer["price"] < old_price else "UP"
                    )
                    print(
                        f"PRICE {direction}: {model} / "
                        f"{offer['shop']}: {old_price:.2f} -> "
                        f"{offer['price']:.2f} {offer['currency']}"
                    )

                alert_pln = cfg.get("alert_pln")

                if (
                    offer["currency"] == "PLN"
                    and alert_pln is not None
                    and offer["price"] <= alert_pln
                ):
                    print(
                        f"ALERT: {model} / {offer['shop']} = "
                        f"{offer['price']:.2f} PLN "
                        f"<= {alert_pln:.2f} PLN"
                    )

            model_rows = [
                row for row in current_rows
                if row["model"] == model
            ]
            pln_rows = [
                row for row in model_rows
                if row["currency"] == "PLN"
            ]

            if pln_rows:
                cheapest = min(pln_rows, key=lambda x: float(x["price"]))
                print(
                    f"OK: {model}: {len(model_rows)} Polish offers; "
                    f"cheapest PLN {float(cheapest['price']):.2f} "
                    f"at {cheapest['shop']}"
                )
            else:
                print(
                    f"OK: {model}: {len(model_rows)} Polish offers; "
                    "no PLN offer to compare."
                )

        except Exception as exc:
            failed_models += 1
            print(f"ERROR: {model}: {exc}")

    print(
        f"Collection finished: {successful_models}/"
        f"{len(TRACKED_PANELS)} models successful; "
        f"{failed_models} failed."
    )

    if not current_rows:
        print("ERROR: zero Polish offers collected. CSV files unchanged.")
        raise SystemExit(2)

    try:
        history_rows = load_csv(HISTORY_PATH)
    except Exception as exc:
        print(f"ERROR: could not read price history: {exc}")
        raise SystemExit(2)

    # Remove old records that are foreign or whose country is unknown.
    # Keep only records explicitly identified as Poland.
    clean_history = [
        row for row in history_rows
        if is_poland(row.get("country", ""))
    ]

    # New records are labelled Poland only after passing the checks above.
    updated_history = clean_history + current_rows

    temp_current = None
    temp_history = None

    try:
        temp_current = write_csv_temp(CSV_PATH, current_rows)
        temp_history = write_csv_temp(HISTORY_PATH, updated_history)

        os.replace(temp_current, CSV_PATH)
        temp_current = None

        os.replace(temp_history, HISTORY_PATH)
        temp_history = None

    except Exception as exc:
        print(f"ERROR: could not save CSV files: {exc}")
        raise SystemExit(2)

    finally:
        for temp_path in (temp_current, temp_history):
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    removed_history = len(history_rows) - len(clean_history)

    print(f"Saved {len(current_rows)} Polish offers to {CSV_PATH}.")
    print(
        f"History contains {len(updated_history)} Polish records "
        f"in {HISTORY_PATH}."
    )
    print(f"Removed {removed_history} non-Polish/unknown history records.")
    print(f"Total Polish offers parsed: {total_offers}")
    print(
        f"Models successful: {successful_models}/"
        f"{len(TRACKED_PANELS)}"
    )
    print(f"Models failed: {failed_models}")

    if failed_models:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
