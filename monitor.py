#!/usr/bin/env python3
"""PV price monitor: Polish offers from direct shops, Allegro and ComparePV.

Uses requests + Jina Reader (no HTML parser dependency). Search results are treated
as candidates only; exact model-token checks and price-basis checks are applied
before a row is accepted. A fetch/parse error never means out-of-stock.
"""
from __future__ import annotations

import csv
import os
import re
import tempfile
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

import requests

# Keep exact catalog symbols here. Do not remove suffixes such as _BFT, _BF or -BDV.
TRACKED_PANELS = {
    "JA Solar JAM54D40-465/LB_BFT": {
        "compare_url": "https://comparepv.com/panel/ja-solar-jam54d40-465-lb",
        "search": "JAM54D40-465/LB_BFT", "alert_pln": 340.0,
    },
    "JA Solar JAM54D40-465/LR": {
        "compare_url": "https://comparepv.com/panel/ja-solar-jam54d40-465-lr",
        "search": "JAM54D40-465/LR", "alert_pln": 325.0,
    },
    "JA Solar JAM54D40-460/LB": {
        "compare_url": "https://comparepv.com/panel/ja-solar-jam54d40-460-lb-1",
        "search": "JAM54D40-460/LB", "alert_pln": 315.0,
    },
    "JA Solar JAM54D40-460/LR": {
        "compare_url": "https://comparepv.com/panel/ja-solar-jam54d40-460-lr",
        "search": "JAM54D40-460/LR", "alert_pln": 315.0,
    },
    "AIKO-A470-MCE54Mw": {
        "compare_url": "https://comparepv.com/panel/aiko-aiko-a470-mce54mw",
        "search": "AIKO-A470-MCE54Mw", "alert_pln": None,
    },
    "AIKO-A475-MCE54Mw": {
        "compare_url": "https://comparepv.com/panel/aiko-aiko-a475-mce54mw",
        "search": "AIKO-A475-MCE54Mw", "alert_pln": None,
    },
    "AIKO-A485-MCE54Mw": {
        "compare_url": "https://comparepv.com/panel/aiko-aiko-a485-mce54mw",
        "search": "AIKO-A485-MCE54Mw", "alert_pln": None,
    },
    "Trina Solar TSM-465NEG9R.28": {
        "compare_url": "https://comparepv.com/offers?country=PL&q=TSM-465NEG9R.28",
        "search": "TSM-465NEG9R.28", "alert_pln": None,
    },
    # This is the broad family code in the existing monitor. Exact matching is
    # deliberately strict; update this key when the exact Jinko SKU is chosen.
    "Jinko Solar JKM450-475N-48HL4M-DB-Z2-EN (465W)": {
        "compare_url": "https://comparepv.com/panel/jinkosolar-jkm450-475n-48hl4m-db-z2-en-465w",
        "search": "JKM450-475N-48HL4M-DB-Z2-EN", "alert_pln": None,
    },
    "LONGi LR7-54HTH-465M": {
        "compare_url": "https://comparepv.com/panel/longi-lr7-54hth-465m",
        "search": "LR7-54HTH-465M", "alert_pln": None,
    },
}

CSV_PATH = Path("data/prices.csv")
HISTORY_PATH = Path("data/price_history.csv")
JINA_PREFIX = "https://r.jina.ai/"
QTY_TARGET = 16
VAT_RATE = 0.23

# New columns are appended to the previous schema. Existing CSV files are migrated
# on read, preserving old rows and their timestamps.
FIELDNAMES = [
    "checked_at_utc", "model", "shop", "country", "price", "currency",
    "vat", "stock", "updated", "source_url", "source_type",
    "price_basis", "qty16_cost_pln", "shipping", "stale_note",
]
OLD_FIELDNAMES = FIELDNAMES[:10]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; PV-Price-Monitor/2.0; +https://github.com/blotunga-prog/pv-price-monitor)",
    "Accept": "text/plain, text/markdown;q=0.9, */*;q=0.8",
})

# Source search URLs. They are discovery endpoints, not purchase confirmation.
def source_urls(term: str) -> list[tuple[str, str, str]]:
    q = quote(term)
    return [
        ("Megawat Elektrohurt", "direct_shop", f"https://megawat-elektrohurt.pl/?s={q}&post_type=product"),
        ("PVHurt", "direct_shop", f"https://pvhurt.pl/?s={q}"),
        ("PVsklep", "direct_shop", f"https://pvsklep.pl/?s={q}"),
        ("Allegro", "marketplace", f"https://allegro.pl/listing?string={q}"),
    ]


def clean(text: object) -> str:
    s = unescape(str(text or "").strip())
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 \2", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("**", "").replace("__", "")
    return re.sub(r"\s+", " ", s).strip()


def norm(text: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def is_poland(text: object) -> bool:
    s = clean(text).lower()
    return s in {"pl", "pol", "poland", "polska", "polska (pl)"} or "polska" in s or "poland" in s or "🇵🇱" in s


def parse_number(raw: str) -> float | None:
    s = raw.replace("\u00a0", " ").replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.rsplit(",", 1)[-1]
        s = s.replace(",", ".") if len(tail) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1:
        p = s.split(".")
        s = "".join(p[:-1]) + "." + p[-1]
    try:
        return float(s)
    except ValueError:
        return None


PRICE_RE = re.compile(r"(?<![A-Z0-9])([0-9][0-9\s\u00a0.,]{0,14})\s*(zł|PLN|EUR|€|USD|\$)", re.I)


def price_in_context(text: str, source_type: str) -> tuple[float, str, str] | None:
    """Return (price, currency, basis). Prefer explicitly gross prices.

    Net-only prices are converted to gross at 23% and labelled calculated.
    Unlabelled prices from consumer Allegro are assumed gross; unlabelled prices
    from wholesale/search/comparison pages are not accepted as gross.
    """
    matches = []
    for m in PRICE_RE.finditer(text):
        value = parse_number(m.group(1))
        if value is None or value <= 0:
            continue
        currency = m.group(2).upper()
        currency = {"ZŁ": "PLN", "€": "EUR", "$": "USD"}.get(currency, currency)
        around = text[max(0, m.start()-35):min(len(text), m.end()+45)].lower()
        if re.search(r"\bbrutto\b|gross", around):
            matches.append((value, currency, "gross_explicit"))
        elif re.search(r"\bnetto\b|\bnet\b", around):
            matches.append((round(value * (1 + VAT_RATE), 2), currency, "gross_calculated_from_net_23pct"))
        elif source_type == "marketplace" and currency == "PLN":
            matches.append((value, currency, "gross_assumed_consumer_price"))
    # Explicit gross wins over converted net; otherwise take the first valid price.
    for wanted in ("gross_explicit", "gross_calculated_from_net_23pct", "gross_assumed_consumer_price"):
        for item in matches:
            if item[2] == wanted:
                return item
    return None


def get_reader_text(url: str) -> str:
    headers = {}
    key = os.getenv("JINA_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    response = SESSION.get(JINA_PREFIX + url, headers=headers, timeout=50)
    response.raise_for_status()
    body = response.text
    if len(body) < 80:
        raise RuntimeError(f"Za krótka odpowiedź ({len(body)} znaków)")
    return body


def exact_model_match(text: str, search_term: str) -> bool:
    """Match the complete model token, including its suffix.

    Separators between model chunks may differ (slash, hyphen, underscore or
    whitespace), but an alphanumeric suffix after the requested SKU is rejected.
    Thus /LB does not match /LB_BFT and -BDV does not match another suffix.
    """
    chunks = re.findall(r"[A-Z0-9]+", search_term.upper())
    if not chunks:
        return False
    pattern = r"(?<![A-Z0-9])" + r"[\s._/\-]*".join(map(re.escape, chunks)) + r"(?![A-Z0-9]|[._/\-][A-Z0-9])"
    return re.search(pattern, str(text).upper()) is not None


def extract_links(text: str) -> list[str]:
    return re.findall(r"https?://[^\s)\]>]+", text)


def guess_stock(text: str) -> str:
    s = text.lower()
    if any(x in s for x in ("brak w magazynie", "niedostępny", "wyprzedany", "out of stock", "unavailable")):
        return "unavailable"
    m = re.search(r"(?:dostępność|w magazynie|stan magazynowy|available|stock)[^\n]{0,55}", s, re.I)
    if m:
        return clean(m.group(0))[:100]
    if "dostępny" in s or "in stock" in s or "produkt dostępny" in s:
        return "available (quantity not confirmed)"
    return "unknown"


def parse_search_page(text: str, model: str, search_term: str, shop: str,
                      source_type: str, source_url: str) -> list[dict]:
    """Find snippets containing the exact catalog symbol and a nearby price.

    A product card usually occupies several lines in Jina markdown. Candidate
    snippets are checked independently so one unrelated price elsewhere on the
    search page cannot be assigned to the requested panel.
    """
    lines = text.splitlines()
    out = []
    seen = set()
    for i, line in enumerate(lines):
        # Look at a short card window; require exact model token in that window.
        start, end = max(0, i - 1), min(len(lines), i + 7)
        snippet = "\n".join(lines[start:end])
        if not exact_model_match(snippet, search_term):
            continue
        parsed = price_in_context(clean(snippet), source_type)
        if not parsed:
            continue
        price, currency, basis = parsed
        # A model name by itself plus a price far away is not enough.
        if currency != "PLN":
            continue
        links = extract_links(snippet)
        product_url = next((u for u in links if urlparse(u).netloc and not u.startswith(JINA_PREFIX)), source_url)
        key = (round(price, 2), product_url)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "model": model,
            "shop": shop,
            "country": "Poland",
            "price": price,
            "currency": currency,
            "vat": "23%" if basis == "gross_calculated_from_net_23pct" else ("included/assumed" if basis.startswith("gross") else "unknown"),
            "stock": guess_stock(snippet),
            "updated": "not provided by source",
            "source_url": product_url,
            "source_type": source_type,
            "price_basis": basis,
            "qty16_cost_pln": f"{price * QTY_TARGET:.2f}",
            "shipping": "not included / verify for Łęknica",
            "stale_note": "",
        })
    return out


def parse_comparepv(text: str, model: str, source_url: str) -> list[dict]:
    """Parse ComparePV tables as discovery only, not purchase-confirmed prices."""
    rows = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        header = [clean(c).lower() for c in line.strip().strip("|").split("|")]
        if not {"shop", "price", "stock"}.issubset(set(header)):
            continue
        if not any("offer updated" in h for h in header):
            continue
        if i + 1 >= len(lines) or not re.fullmatch(r"\|?[\s:|\-]+\|?", lines[i+1].strip()):
            continue
        idx = {h: header.index(h) for h in header}
        country_i = idx.get("country")
        product_i = idx.get("product")
        for rowline in lines[i+2:]:
            if not rowline.strip().startswith("|"):
                break
            cells = [clean(c) for c in rowline.strip().strip("|").split("|")]
            if len(cells) != len(header):
                break
            country = cells[country_i] if country_i is not None else ("Poland" if parse_qs(urlparse(source_url).query).get("country", [""])[0].upper() == "PL" else "")
            if not is_poland(country):
                continue
            if product_i is not None and not exact_model_match(cells[product_i], TRACKED_PANELS[model]["search"]):
                continue
            # ComparePV often does not identify whether the price is gross.
            price_text = cells[idx["price"]]
            match = PRICE_RE.search(price_text)
            if not match:
                continue
            price = parse_number(match.group(1))
            currency = match.group(2).upper()
            currency = {"ZŁ": "PLN", "€": "EUR", "$": "USD"}.get(currency, currency)
            if price is None or currency != "PLN":
                continue
            basis = "unverified_comparison_price"
            rows.append({
                "model": model, "shop": cells[idx["shop"]], "country": "Poland",
                "price": price, "currency": currency, "vat": "unknown",
                "stock": cells[idx["stock"]],
                "updated": cells[idx[next(h for h in header if "offer updated" in h)]],
                "source_url": source_url, "source_type": "comparison_discovery",
                "price_basis": "unverified_comparison_price",
                "qty16_cost_pln": "", "shipping": "not included / verify for Łęknica",
                "stale_note": "Discovery only: confirm price, VAT, stock and shipping at the seller.",
            })
    return rows


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for old in reader:
            row = {k: (old.get(k) or "") for k in FIELDNAMES}
            if not row["source_type"]:
                row["source_type"] = "legacy_comparepv"
            if not row["price_basis"]:
                row["price_basis"] = "legacy_basis_unknown"
            if not row["qty16_cost_pln"]:
                try:
                    row["qty16_cost_pln"] = f"{float(row['price']) * QTY_TARGET:.2f}" if row["currency"] == "PLN" else ""
                except (ValueError, TypeError):
                    pass
            rows.append(row)
        return rows


def write_csv_atomic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def row_key(row: dict) -> tuple[str, str, str, str]:
    return (row.get("model", ""), row.get("shop", ""), row.get("currency", ""), row.get("source_type", ""))


def main() -> None:
    try:
        old_current = load_csv(CSV_PATH)
        history = load_csv(HISTORY_PATH)
    except Exception as exc:
        print(f"ERROR: nie można odczytać CSV: {exc}")
        raise SystemExit(2)

    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = {}
    for row in old_current:
        try:
            previous[row_key(row)] = float(row["price"])
        except (ValueError, TypeError):
            continue

    fresh_by_model: dict[str, list[dict]] = {}
    errors: list[str] = []
    total = 0

    for model, cfg in TRACKED_PANELS.items():
        model_rows: list[dict] = []
        # Direct sellers and marketplace: exact SKU search.
        for shop, source_type, url in source_urls(cfg["search"]):
            try:
                body = get_reader_text(url)
                rows = parse_search_page(body, model, cfg["search"], shop, source_type, url)
                model_rows.extend(rows)
                if rows:
                    print(f"OK direct: {model} / {shop}: {len(rows)} candidate offer(s)")
                else:
                    print(f"INFO: {model} / {shop}: no verified exact-model gross-price match")
            except Exception as exc:
                errors.append(f"{model} / {shop}: {exc}")
                print(f"WARN: {model} / {shop}: {exc}")
        # ComparePV is discovery only; do not treat these prices as confirmed gross.
        try:
            body = get_reader_text(cfg["compare_url"])
            comparison_rows = parse_comparepv(body, model, cfg["compare_url"])
            model_rows.extend(comparison_rows)
            print(f"OK ComparePV: {model}: {len(comparison_rows)} discovery offer(s)")
        except Exception as exc:
            errors.append(f"{model} / ComparePV: {exc}")
            print(f"WARN: {model} / ComparePV: {exc}")

        # De-duplicate identical source/product/price records.
        unique = {}
        for row in model_rows:
            row["checked_at_utc"] = checked
            unique[(row["shop"], row["source_url"], row["price"], row["source_type"])] = row
        model_rows = list(unique.values())
        if model_rows:
            fresh_by_model[model] = model_rows
            total += len(model_rows)
            for row in model_rows:
                if row["price_basis"] in {"gross_explicit", "gross_calculated_from_net_23pct", "gross_assumed_consumer_price"} and row["currency"] == "PLN":
                    old = previous.get(row_key(row))
                    if old is not None and abs(old - float(row["price"])) >= 0.01:
                        direction = "DOWN" if float(row["price"]) < old else "UP"
                        print(f"PRICE {direction}: {model} / {row['shop']}: {old:.2f} -> {float(row['price']):.2f} PLN")
                    threshold = cfg.get("alert_pln")
                    if threshold is not None and float(row["price"]) <= threshold:
                        print(f"ALERT: {model} / {row['shop']} = {float(row['price']):.2f} PLN <= {threshold:.2f} PLN")
        else:
            print(f"WARN: {model}: no offers accepted; previous data will be retained as stale")

    # Current prices: replace only models with fresh accepted rows. Preserve stale rows
    # for failed models and explicitly label them stale rather than implying freshness.
    current = []
    for row in old_current:
        model = row.get("model", "")
        # Current CSV contains only models currently configured. Old model names
        # remain in price_history.csv, but not as misleading current offers.
        if model not in TRACKED_PANELS:
            continue
        if model not in fresh_by_model:
            row["stale_note"] = "NOT REFRESHED: last known value; verify before purchase."
            current.append(row)
    for model in TRACKED_PANELS:
        current.extend(fresh_by_model.get(model, []))

    # Keep prior history and append only newly fetched records.
    new_history = [r for model in TRACKED_PANELS for r in fresh_by_model.get(model, [])]
    try:
        write_csv_atomic(CSV_PATH, current)
        write_csv_atomic(HISTORY_PATH, history + new_history)
    except Exception as exc:
        print(f"ERROR: zapis CSV nie powiódł się: {exc}")
        raise SystemExit(2)

    print(f"Saved current offers: {len(current)} to {CSV_PATH}")
    print(f"Added history records: {len(new_history)}; total history: {len(history) + len(new_history)}")
    print(f"Accepted fresh offers: {total}")
    print(f"Source errors / no-fetch events: {len(errors)}")
    if errors:
        print("Source issues (some can be temporary blocks or no matching offer):")
        for err in errors[:40]:
            print(f" - {err}")
        # Nonzero exit keeps GitHub Actions visibly red while CSV commit step still runs.
        raise SystemExit(2)


if __name__ == "__main__":
    main()
