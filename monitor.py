#!/usr/bin/env python3
import csv
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(file).resolve().parent
DATA_DIR = ROOT / "data"
PRICES_FILE = DATA_DIR / "prices.csv"
HISTORY_FILE = DATA_DIR / "price_history.csv"

FIELDS = [
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
"source_type",
"price_basis",
"qty16_cost_pln",
"shipping",
"stale_note",
]

DIRECT_SOURCES = [
(
"Megawat Elektrohurt",
"https://megawat-elektrohurt.pl/?s={q}&post_type=product",
),
("PVhurt", "https://pvhurt.pl/?s={q}"),
("pvsklep.pl", "https://pvsklep.pl/?s={q}"),
("Allegro", "https://allegro.pl/listing?string={q}"),
]

PRODUCT_URLS = {
(
"JA Solar JAM54D40-465/LB_BFT",
"Megawat Elektrohurt",
): (
"https://megawat-elektrohurt.pl/p/2153/5488571/"
"panel-pv-465wp-ja-solar-jam54d40-465-lb-bft-"
"n-type-bifacial-transparent-czarna-rama-"
"panele-fotowoltaiczne-bifacial-panele-pv-fotowoltaika.html"
),
}

PANELS = {
"JA Solar JAM54D40-465/LB_BFT": {
"search": "JAM54D40-465/LB_BFT",
"aliases": ["JAM54D40-465/LB_BFT"],
"compare": "https://comparepv.com/panel/ja-solar-jam54d40-465-lb",
"alert": 340.00,
},
"JA Solar JAM54D40-465/LR": {
"search": "JAM54D40-465/LR",
"aliases": ["JAM54D40-465/LR"],
"compare": "https://comparepv.com/panel/ja-solar-jam54d40-465-lr",
"alert": 325.00,
},
"JA Solar JAM54D40-460/LB": {
"search": "JAM54D40-460/LB",
"aliases": ["JAM54D40-460/LB"],
"compare": "https://comparepv.com/panel/ja-solar-jam54d40-460-lb-1",
"alert": 315.00,
},
"JA Solar JAM54D40-460/LR": {
"search": "JAM54D40-460/LR",
"aliases": ["JAM54D40-460/LR"],
"compare": "https://comparepv.com/panel/ja-solar-jam54d40-460-lr",
"alert": 315.00,
},
"AIKO-A470-MCE54Mw": {
"search": "AIKO-A470-MCE54Mw",
"aliases": ["A470-MCE54Mw"],
"compare": None,
"alert": None,
},
"AIKO-A475-MCE54Mw": {
"search": "AIKO-A475-MCE54Mw",
"aliases": ["A475-MCE54Mw"],
"compare": None,
"alert": None,
},
"AIKO-A485-MCE54Mw": {
"search": "AIKO-A485-MCE54Mw",
"aliases": ["A485-MCE54Mw"],
"compare": None,
"alert": None,
},
"Trina Solar TSM-465NEG9R.28": {
"search": "TSM-465NEG9R.28",
"aliases": ["TSM-465NEG9R.28"],
"compare": None,
"alert": None,
},
"Jinko Solar JKM450-475N-48HL4M-DB-Z2-EN (465W)": {
"search": "JKM450-475N-48HL4M-DB-Z2-EN",
"aliases": ["JKM450-475N-48HL4M-DB-Z2-EN"],
"compare": None,
"alert": None,
},
"LONGi LR7-54HTH-465M": {
"search": "LR7-54HTH-465M",
"aliases": ["LR7-54HTH-465M"],
"compare": "https://comparepv.com/panel/longi-lr7-54hth-465m",
"alert": None,
},
}

TIMEOUT = 30
SESSION = requests.Session()
SESSION.headers.update({
"User-Agent": (
"Mozilla/5.0 (X11; Linux x86_64) "
"AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
),
"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
})

def log(message):
    print(message, flush=True)

def now_utc():
    return datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat()

def norm(value):
    value = (value or "").replace("\u00a0", " ")
    value = value.replace("\u202f", " ")
return re.sub(r"\s+", " ", value).strip().casefold()

def atomic_write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)

fd, temp_name = tempfile.mkstemp(
    prefix=path.name + ".",
    suffix=".tmp",
    dir=path.parent,
)

try:
    with os.fdopen(
        fd, "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()

        for row in rows:
            writer.writerow({
                field: row.get(field, "")
                for field in fields
            })

    os.replace(temp_name, path)

except Exception:
    try:
        os.unlink(temp_name)
    except OSError:
        pass
    raise

def read_csv(path):
    if not path.exists():
        return []

with path.open(
    "r", newline="", encoding="utf-8-sig"
) as handle:
    return list(csv.DictReader(handle))

def csv_fields(base_fields, rows):
    result = list(base_fields)

for row in rows:
    for field in row:
        if field and field not in result:
            result.append(field)

return result

def exact_model_match(text, info):
    haystack = norm(text).replace(" ", "")

for alias in info["aliases"]:
    needle = norm(alias).replace(" ", "")
    if needle and needle in haystack:
        return True

return False

def parse_number(raw):
    raw = raw.replace("\u00a0", " ")
    raw = re.sub(r"\s+", "", raw)

if "," in raw and "." in raw:
    if raw.rfind(",") > raw.rfind("."):
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
elif "," in raw:
    raw = raw.replace(",", ".")

try:
    return float(raw)
except ValueError:
    return None

PRICE_RE = re.compile(
r"(?<![\w])"
r"(\d{1,3}(?:[ .]\d{3})(?:[,.]\d{2})|"
r"\d{2,5}(?:[,.]\d{1,2})?)"
r"\s(?:zł|PLN)\b",
re.IGNORECASE,
)

def model_context(text, info):
    lines = (text or "").splitlines()
aliases = [
norm(alias).replace(" ", "")
for alias in info["aliases"]
]

matches = []

for index, line in enumerate(lines):
    normalized_line = norm(line).replace(" ", "")

    if any(alias and alias in normalized_line for alias in aliases):
        priority = 0 if re.search(
            r"(model|kod producenta|mpn|kod dostawcy)",
            line,
            re.IGNORECASE,
        ) else 1
        matches.append((priority, index))

if not matches:
    return text or ""

matches.sort()
_, index = matches[0]

start = max(0, index - 15)
end = min(len(lines), index + 23)

return "\n".join(lines[start:end])

def explicit_gross_price(text):
    lines = (text or "").splitlines()

gross_re = re.compile(
    r"(brutto|cena\s+z\s+vat|z\s+vat|vat\s+wliczony|"
    r"including\s+vat|gross\s+price)",
    re.IGNORECASE,
)
net_re = re.compile(
    r"(netto|bez\s+vat|net\s+price|excl\.?\s*vat)",
    re.IGNORECASE,
)

for line in lines:
    if not gross_re.search(line) or net_re.search(line):
        continue

    for match in PRICE_RE.finditer(line):
        price = parse_number(match.group(1))
        if price is not None and 10 <= price <= 10000:
            return round(price, 2)

for index, line in enumerate(lines):
    if net_re.search(line):
        continue

    matches = list(PRICE_RE.finditer(line))
    if not matches:
        continue

    start = max(0, index - 1)
    end = min(len(lines), index + 2)
    context = " ".join(lines[start:end])

    if not gross_re.search(context):
        continue

    if net_re.search(context):
        continue

    price = parse_number(matches[0].group(1))
    if price is not None and 10 <= price <= 10000:
        return round(price, 2)

return None

def stock_quantity(text):
    patterns = [
r"(\d[\d ])\sszt.?",
r"(\d[\d ])\spcs\b",
]

for line in (text or "").splitlines():
    if not re.search(
        r"(magazyn|dostęp|dostep|stan w feedzie)",
        line,
        re.IGNORECASE,
    ):
        continue

    for pattern in patterns:
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", "", match.group(1))

return ""

def stock_status(text):
    value = norm(text)

if any(term in value for term in (
    "brak w magazynie",
    "niedostępny",
    "niedostepny",
    "out of stock",
)):
    return "Out of stock"

if any(term in value for term in (
    "w magazynie",
    "produkt dostępny",
    "produkt dostepny",
    "dostępny",
    "dostepny",
    "in stock",
    "na stanie",
)):
    quantity = stock_quantity(text)
    if quantity:
        return f"In stock ({quantity} szt.)"
    return "In stock"

return "Not stated"

def shipping_info(text):
    for line in (text or "").splitlines():
        if not re.search(r"wysyłka\s+od|wysylka\s+od",line, re.IGNORECASE):
        	continue

    match = PRICE_RE.search(line)
    if match:
        price = parse_number(match.group(1))
        if price is not None:
            return (
                f"od {price:.2f} PLN; "
                "koszt dostawy 16 szt. do Łęknicy do potwierdzenia"
            )

return "Nie odczytano; potwierdzić dla 16 szt. do Łęknicy"

def fetch_text(url):
	reader_url = "https://r.jina.ai/" + url

headers = {}
api_key = os.getenv("JINA_API_KEY", "").strip()

if api_key:
    headers["Authorization"] = "Bearer " + api_key

response = SESSION.get(
    reader_url,
    headers=headers,
    timeout=TIMEOUT,
)
response.raise_for_status()

if not response.text.strip():
    raise ValueError("Pobrana strona nie zawiera tekstu")

return response.text, response.status_code, response.url

def parse_direct(text, model, info, shop, url, checked_at):
	if not exact_model_match(text, info):
		return None, "nie znaleziono dokładnego kodu modelu"

context = model_context(text, info)
price = explicit_gross_price(context)

if price is None:
    return None, (
        "znaleziono kod modelu, ale nie ma jednoznacznej "
        "ceny brutto w PLN w jego kontekście"
    )

return {
    "checked_at_utc": checked_at,
    "model": model,
    "shop": shop,
    "country": "Poland",
    "price": f"{price:.2f}",
    "currency": "PLN",
    "vat": "gross",
    "stock": stock_status(context),
    "updated": "",
    "source_url": url,
    "source_type": "direct_store",
    "price_basis": "explicit_gross_price",
    "qty16_cost_pln": f"{price * 16:.2f}",
    "shipping": shipping_info(context),
    "stale_note": "",
}, "dokładny model i jawna cena brutto"

KNOWN_SHOPS = [
"Megawat Elektrohurt",
"PVhurt",
"pvsklep.pl",
"Solar-EM",
"Solmix",
"PV Zenit",
"Eco System Projekt",
"Solaris Energy",
"Sklep Soltech",
"Allegro",
]

def parse_comparepv(text, model, info, checked_at):
	results = []
lines = (text or "").splitlines()

if not exact_model_match(text, info):
    return results

for index, line in enumerate(lines):
    context = " ".join(
        lines[max(0, index - 1):min(len(lines), index + 3)]
    )

    if not re.search(r"(?:PLN|zł)", context, re.IGNORECASE):
        continue

    shop = next(
        (
            name for name in KNOWN_SHOPS
            if norm(name) in norm(context)
        ),
        None,
    )

    if not shop:
        continue

    matches = list(PRICE_RE.finditer(context))
    if not matches:
        continue

    price = parse_number(matches[0].group(1))
    if price is None or not 10 <= price <= 10000:
        continue

    updated_match = re.search(
        r"\b(?:\d+\s*(?:min|minutes?|godz\.?|godzin|h|dni|days)"
        r"\s*ago|\d{4}-\d{2}-\d{2})\b",
        context,
        re.IGNORECASE,
    )

    results.append({
        "checked_at_utc": checked_at,
        "model": model,
        "shop": shop,
        "country": "Poland",
        "price": f"{price:.2f}",
        "currency": "PLN",
        "vat": "unknown",
        "stock": stock_status(context),
        "updated": (
            updated_match.group(0) if updated_match else ""
        ),
        "source_url": info["compare"],
        "source_type": "comparison_discovery",
        "price_basis": "unverified_comparison_price",
        "qty16_cost_pln": "",
        "shipping": "Niepotwierdzona; sprawdzić u sprzedawcy",
        "stale_note": (
            "Discovery only: confirm exact model, price, VAT, "
            "stock and shipping at the seller."
        ),
    })

unique = {}
for row in results:
    unique[(row["shop"], row["price"])] = row

return list(unique.values())

def identity(row):
	return (
row.get("model", ""),
row.get("shop", ""),
row.get("source_type", ""),
row.get("source_url", ""),
)

def mark_stale(row, checked_at, reason):
	result = dict(row)

old_note = (result.get("stale_note") or "").strip()
new_note = "Nie odświeżono: " + reason

if new_note not in old_note:
    result["stale_note"] = (
        old_note + " | " + new_note
    ).strip(" |")

return result

def main():
	checked_at = now_utc()

old_prices = read_csv(PRICES_FILE)
old_history = read_csv(HISTORY_FILE)

fresh_rows = []
history_additions = []
failed_sources = 0
direct_count = 0
discovery_count = 0

successful_direct_fetch = set()
successful_compare_fetch = set()

for model, info in PANELS.items():
    log(f"\n=== {model} ===")

    for shop, template in DIRECT_SOURCES:
        product_url = PRODUCT_URLS.get((model, shop))

        if product_url:
            url = product_url
            url_kind = "product page"
        else:
            url = template.format(q=quote(info["search"]))
            url_kind = "search page"

        try:
            text, status, final_url = fetch_text(url)
            successful_direct_fetch.add((model, shop))

            row, reason = parse_direct(
                text, model, info, shop, url, checked_at
            )

            if row:
                fresh_rows.append(row)
                history_additions.append(row)
                direct_count += 1

                log(
                    f"OK direct ({url_kind}): {shop}: "
                    f"{row['price']} PLN brutto; "
                    f"stan: {row['stock']}; "
                    f"wysyłka: {row['shipping']}"
                )
            else:
                log(
                    f"INFO direct ({url_kind}): {shop}: {reason}; "
                    f"HTTP={status}; tekst={len(text)} znaków"
                )

        except Exception as exc:
            failed_sources += 1
            log(
                f"WARN direct: {shop}: "
                f"{type(exc).__name__}: {exc}"
            )

    compare_url = info.get("compare")

    if compare_url:
        try:
            text, status, final_url = fetch_text(compare_url)
            successful_compare_fetch.add(model)

            rows = parse_comparepv(
                text, model, info, checked_at
            )

            fresh_rows.extend(rows)
            history_additions.extend(rows)
            discovery_count += len(rows)

            if rows:
                log(
                    f"OK ComparePV: {len(rows)} "
                    "ofert odkrywczych; ceny niepotwierdzone"
                )
            else:
                log(
                    "INFO ComparePV: nie udało się odczytać "
                    f"wierszy ofert; HTTP={status}; "
                    f"tekst={len(text)} znaków"
                )

        except Exception as exc:
            failed_sources += 1
            log(
                f"WARN ComparePV: "
                f"{type(exc).__name__}: {exc}"
            )

    threshold = info.get("alert")

    if threshold is not None:
        for row in fresh_rows:
            if row["model"] != model:
                continue

            if row.get("source_type") != "direct_store":
                continue

            if row.get("vat") != "gross":
                continue

            try:
                price = float(row["price"])
            except (TypeError, ValueError):
                continue

            if price <= threshold:
                log(
                    f"ALERT POTWIERDZONY: {model}, "
                    f"{row['shop']}, {price:.2f} PLN brutto "
                    f"(próg {threshold:.2f} PLN)"
                )

current_by_identity = {}

for row in fresh_rows:
    current_by_identity[identity(row)] = row

fresh_direct_shops = {
    (row["model"], row["shop"])
    for row in fresh_rows
    if row["source_type"] == "direct_store"
}

fresh_compare_models = {
    row["model"]
    for row in fresh_rows
    if row["source_type"] == "comparison_discovery"
}

for old in old_prices:
    model = old.get("model", "")
    shop = old.get("shop", "")
    source_type = old.get("source_type", "")

    if source_type == "direct_store":
        key = (model, shop)

        if key in fresh_direct_shops:
            continue

        if key in successful_direct_fetch:
            reason = (
                "pobranie działa, ale nie znaleziono "
                "zweryfikowanej ceny brutto"
            )
        else:
            reason = "błąd odświeżania źródła"

        stale = mark_stale(old, checked_at, reason)
        current_by_identity.setdefault(identity(stale), stale)

    elif source_type in (
        "comparison_discovery",
        "legacy_comparepv",
    ):
        if model in fresh_compare_models:
            continue

        if model in successful_compare_fetch:
            reason = "nie odczytano aktualnych ofert z ComparePV"
        else:
            reason = "nie udało się odświeżyć ComparePV"

        stale = mark_stale(old, checked_at, reason)
        current_by_identity.setdefault(identity(stale), stale)

    else:
        current_by_identity.setdefault(identity(old), old)

current_rows = list(current_by_identity.values())

price_fields = csv_fields(FIELDS, old_prices)
atomic_write_csv(PRICES_FILE, price_fields, current_rows)

history_rows = list(old_history)
history_rows.extend(history_additions)

history_fields = csv_fields(FIELDS, history_rows)
atomic_write_csv(HISTORY_FILE, history_fields, history_rows)

log("\n=== PODSUMOWANIE ===")
log(f"Aktualne rekordy: {len(current_rows)}")
log(f"Nowe obserwacje bezpośrednie: {direct_count}")
log(f"Nowe obserwacje ComparePV: {discovery_count}")
log(f"Łącznie rekordów historii: {len(history_rows)}")
log(f"Błędy pobierania źródeł: {failed_sources}")
log(
    "ComparePV pozostaje źródłem odkrywczym; "
    "nie jest traktowane jako potwierdzona cena brutto."
)

total_sources = len(PANELS) * len(DIRECT_SOURCES)

if failed_sources >= total_sources and direct_count == 0:
    log("ERROR: sprawdź logi — pobieranie źródeł nie działa.")
    return 2

return 0

if name == "main":
	sys.exit(main())
