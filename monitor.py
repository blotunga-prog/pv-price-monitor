#!/usr/bin/env python3
from __future__ import annotations
import csv, re, sys, time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import requests

TRACKED_PANELS = {
    "JA Solar JAM54D40-465/LB": {"url": "https://comparepv.com/panel/ja-solar-jam54d40-465-lb", "alert_pln": 340.0},
    "JA Solar JAM54D40-465/LR": {"url": "https://comparepv.com/panel/ja-solar-jam54d40-465-lr", "alert_pln": 325.0},
    "JA Solar JAM54D40-460/LB": {"url": "https://comparepv.com/panel/ja-solar-jam54d40-460-lb-1", "alert_pln": 315.0},
    "JA Solar JAM54D40-460/LR": {"url": "https://comparepv.com/panel/ja-solar-jam54d40-460-lr", "alert_pln": 315.0},
}
DATA_FILE = Path("data/prices.csv")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; PVPriceMonitor/4.0)", "Accept": "text/html,application/xhtml+xml"})

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.in_table=False; self.in_row=False; self.in_cell=False
        self.current_cell=[]; self.current_row=[]; self.tables=[]; self.table_rows=[]; self.cell_tag=None
    def handle_starttag(self, tag, attrs):
        tag=tag.lower()
        if tag=="table" and not self.in_table:
            self.in_table=True; self.table_rows=[]
        elif self.in_table and tag=="tr":
            self.in_row=True; self.current_row=[]
        elif self.in_row and tag in ("td","th"):
            self.in_cell=True; self.cell_tag=tag; self.current_cell=[]
    def handle_data(self, data):
        if self.in_cell: self.current_cell.append(data)
    def handle_endtag(self, tag):
        tag=tag.lower()
        if self.in_cell and tag==self.cell_tag:
            self.current_row.append(" ".join(" ".join(self.current_cell).split())); self.current_cell=[]; self.in_cell=False; self.cell_tag=None
        elif self.in_row and tag=="tr":
            if self.current_row: self.table_rows.append(self.current_row)
            self.current_row=[]; self.in_row=False
        elif self.in_table and tag=="table":
            if self.table_rows: self.tables.append(self.table_rows)
            self.table_rows=[]; self.in_table=False

def parse_price(text):
    text=" ".join(text.replace("\u00a0"," ").split())
    m=re.search(r"(?<!\d)(\d[\d\s\u202f]*(?:[.,]\d{1,2})?)\s*(zł|PLN|€|EUR|Kč|CZK)", text, re.I)
    if not m: return None,""
    raw=m.group(1).replace(" ","").replace("\u202f","")
    if "," in raw: raw=raw.replace(".","").replace(",", ".")
    try: return round(float(raw),2),m.group(2)
    except ValueError: return None,""

def parse_offer_table(html, source_url, model):
    parser=TableParser(); parser.feed(html); results=[]
    for table in parser.tables:
        if not table: continue
        headers=[x.casefold() for x in table[0]]
        required=("shop","country","price","stock")
        if not all(any(req in h for h in headers) for req in required): continue
        def col(name):
            for i,h in enumerate(headers):
                if name in h: return i
            return None
        si,ci,pi,sti,ui=col("shop"),col("country"),col("price"),col("stock"),col("offer updated")
        for row in table[1:]:
            if si is None or pi is None or si>=len(row) or pi>=len(row): continue
            shop=row[si].strip(); country=row[ci].strip() if ci is not None and ci<len(row) else ""
            price_text=row[pi]; stock=row[sti].strip() if sti is not None and sti<len(row) else ""; updated=row[ui].strip() if ui is not None and ui<len(row) else ""
            price,currency=parse_price(price_text)
            if not shop or price is None: continue
            low=price_text.casefold(); vat="incl. VAT" if "incl. vat" in low else "excl. VAT" if "excl. vat" in low else ""
            results.append({"model":model,"shop":shop,"country":country,"price":price,"currency":currency,"vat":vat,"stock":stock,"updated":updated,"source_url":source_url})
        if results: break
    return results

def main():
    checked=datetime.now(timezone.utc).isoformat(); all_rows=[]; errors=[]
    for model,cfg in TRACKED_PANELS.items():
        try:
            r=SESSION.get(cfg["url"],timeout=30); r.raise_for_status(); rows=parse_offer_table(r.text,cfg["url"],model); all_rows.extend(rows)
            pl=[x for x in rows if x["currency"].casefold() in {"zł","pln"} and any(s in x["country"].casefold() for s in ("poland","polska"))]
            if pl:
                c=min(pl,key=lambda x:x["price"]); print(f"{model}: {len(pl)} PL offers; cheapest {c['price']:.2f} zł ({c['shop']})")
            else: print(f"{model}: no Polish offers.")
        except requests.RequestException as e:
            msg=f"{model}: HTTP error: {e}"; print("ERROR: "+msg,file=sys.stderr); errors.append(msg)
        except Exception as e:
            msg=f"{model}: parser error: {e}"; print("ERROR: "+msg,file=sys.stderr); errors.append(msg)
        time.sleep(.7)
    if not all_rows:
        print("ERROR: zero offers were parsed from ComparePV.",file=sys.stderr); print("\n".join(errors),file=sys.stderr); return 2
    DATA_FILE.parent.mkdir(parents=True,exist_ok=True); exists=DATA_FILE.exists()
    fields=["checked_at_utc","model","shop","country","price","currency","vat","stock","updated","source_url"]
    for x in all_rows: x["checked_at_utc"]=checked
    with DATA_FILE.open("a",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields)
        if not exists: w.writeheader()
        w.writerows(all_rows)
    print(f"Saved {len(all_rows)} offers to {DATA_FILE}")
    return 0

if __name__=="__main__": raise SystemExit(main())

