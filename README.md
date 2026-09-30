# PV Price Monitor

Daily monitoring of selected photovoltaic-module offers on ComparePV.

## Files

- `monitor.py` — downloads ComparePV plain-Markdown offer pages, finds tracked modules and stores observations.
- `requirements.txt` — Python dependency.
- `.github/workflows/daily.yml` — runs the monitor every day and commits `data/prices.csv`.

## First run

1. Create a GitHub repository.
2. Upload these files keeping the directory structure.
3. Open **Actions** → **Daily PV price monitor**.
4. Click **Run workflow** for a manual test.
5. After the first successful run, `data/prices.csv` will contain the observed history.

## Changing monitored panels

Edit `TRACKED_MODELS` in `monitor.py`.

Example:

```python
"Jinko JKM455N-48HL4M-DV": [
    "JKM455N-48HL4M-DV",
],
```

## Changing price-alert thresholds

Edit `ALERT_PRICE_PLN`.

The current thresholds are only starting values and should be adjusted after we see the real offer history.

## Notes

ComparePV states that every page is also available as plain Markdown for scripts. The script uses that endpoint rather than scraping browser JavaScript.

The script does not place orders. Prices and stock should always be confirmed on the retailer's own page before purchase.
