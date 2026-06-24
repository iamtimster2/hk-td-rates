"""
Production-grade scraper for Syfe Cash+ Fixed live rates.

Key insight from DOM inspection (28 May 2026):
  - Both HKD and USD calculator blocks render simultaneously in the DOM
    (HKD at ~y=12700, USD at ~y=11900). The "tab" doesn't toggle visibility,
    it just visually highlights one section. So tab-clicking is unreliable
    and unnecessary.
  - Each block has 4 tenor rows in the form:
      "<N> months  <rate> % p.a.  <CCY>$ <returns>"
    where CCY is HK or US.

Strategy:
  1. Render the page (JS is required — the rate cards are empty without it).
  2. Read the full innerText.
  3. Find all rows matching the "<N> months <rate>% p.a. <CCY>$ <amount>"
     pattern. The currency is encoded in the same row, so there's no
     cross-contamination risk between HKD and USD.
  4. Sanity-check: we expect exactly 4 rates per currency at 1M/3M/6M/12M.
     If we get fewer, the page structure has changed — fail loudly.
"""
from playwright.sync_api import sync_playwright
import json, re, sys, time

URL = "https://www.syfe.com/en-hk/cash-management/cash-plus-fixed"

EXPECTED = {
    "HKD": {"1M": 2.25, "3M": 2.55, "6M": 2.75, "12M": 2.95},
    "USD": {"1M": 3.40, "3M": 3.55, "6M": 3.70, "12M": 3.90},
}

# Match: "<N> months  <rate> % p.a.  <CCY>$ <amount>"
# Currency comes from the $ prefix: HK$ → HKD, US$ → USD.
ROW_RE = re.compile(
    r"(\d{1,2})\s*months?\s+"
    r"(\d+(?:\.\d+)?)\s*%\s*p\.?a\.?\s+"
    r"(HK|US)\$\s*([\d,]+)",
    re.IGNORECASE,
)

CCY_MAP = {"HK": "HKD", "US": "USD"}

def extract_rates(text):
    """Returns {'HKD': {tenor: rate, ...}, 'USD': {...}} from page text."""
    out = {"HKD": {}, "USD": {}}
    for m in ROW_RE.finditer(text):
        tenor = m.group(1) + "M"
        rate  = float(m.group(2))
        ccy   = CCY_MAP[m.group(3).upper()]
        if tenor not in ("1M", "3M", "6M", "12M"): continue
        if not (0 < rate < 15): continue
        # First occurrence wins (the calculator widget is canonical)
        if tenor not in out[ccy]:
            out[ccy][tenor] = rate
    return out

def main():
    report = {
        "url": URL,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "playwright headless chromium · currency-anchored regex",
        "expected": EXPECTED,
        "extracted": {},
        "matches": {},
        "result": "unknown",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 1600})
        try:
            page.goto(URL, wait_until="networkidle", timeout=20000)
        except Exception as e:
            report["result"] = "fetch_failed"
            report["error"] = str(e)
            print(json.dumps(report, indent=2)); sys.exit(1)

        # Wait for JS-rendered rates to materialise
        try:
            page.wait_for_function(
                """() => /\\d+(?:\\.\\d+)?\\s*%\\s*p\\.?a\\.?\\s+(?:HK|US)\\$/i.test(document.body.innerText)""",
                timeout=15000,
            )
        except Exception:
            report["result"] = "no_rates_rendered"
            print(json.dumps(report, indent=2)); sys.exit(2)
        page.wait_for_timeout(800)

        # Also scroll down so lazy-loaded sections render
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(400)

        text = page.evaluate("() => document.body.innerText")
        report["extracted"] = extract_rates(text)
        browser.close()

    # Compare
    all_ok = True
    for ccy, expected in EXPECTED.items():
        extracted = report["extracted"].get(ccy, {})
        matches = {}
        for t, exp in expected.items():
            got = extracted.get(t)
            ok = (got is not None and abs(got - exp) < 0.005)
            matches[t] = {"expected": exp, "got": got, "match": ok}
            if not ok: all_ok = False
        report["matches"][ccy] = matches

    # Strict count check: must be exactly 4 per currency
    counts = {ccy: len(report["extracted"].get(ccy, {})) for ccy in ("HKD","USD")}
    report["counts"] = counts
    if counts.get("HKD",0) != 4 or counts.get("USD",0) != 4:
        all_ok = False

    report["result"] = "all_match" if all_ok else "mismatch"
    print(json.dumps(report, indent=2))
    sys.exit(0 if all_ok else 2)

if __name__ == "__main__":
    main()
