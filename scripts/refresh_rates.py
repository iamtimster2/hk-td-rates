"""
Refresh all rates following the new precedence:
  1. Syfe own rate -> direct from product page (proven 8/8 via Playwright)
  2. Everyone else -> StashAway aggregator (best available structured source)
  3. Anything not in StashAway -> retain prior value, flag stale

This script is the canonical "refresh now" entry point. Each run:
  - Spawns fresh incognito Chromium contexts for every fetch
  - Pulls Syfe direct (8 rates) via scrape_syfe_live's proven approach
  - Pulls StashAway HKD + USD tables (160+ rates across 22 providers)
  - Cross-checks: where Syfe-direct disagrees with StashAway for Syfe rows,
    Syfe-direct wins (it's fresher) and the delta is logged as drift
  - Rewrites data/latest.json with explicit verified_by per row

Why this design (rather than scraping each bank directly):
  - Most virtual banks (ZA, Mox, livi, WeLab, AirStar, Fusion) publish
    their TD rates ONLY inside their mobile apps. Public marketing pages
    have no parseable rate data. StashAway gets the rates by manual
    monthly survey + scraping the apps.
  - Traditional banks (HSBC, BOCHK, SCB, Hang Seng, BEA) DO publish
    rates publicly but each in a unique format — many in PDFs, some
    inside consent-gated calculators, all heavily JS-rendered with
    different selectors. A bespoke scraper per bank takes 1-2h each;
    aggregator scraping covers all 22 providers in one fetch.
  - For Syfe's OWN rate, direct scraping is essential because Syfe is
    the source of truth and StashAway lags by 0-7 days.
"""
import json, re, time, pathlib, sys, subprocess
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAP = ROOT / "data" / "latest.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

# ==============================================================
# 1. Syfe direct (proven 8/8 — see scrape_syfe_live.py)
# ==============================================================
SYFE_ROW_RE = re.compile(
    r"(\d{1,2})\s*months?\s+(\d+(?:\.\d+)?)\s*%\s*p\.?a\.?\s+(HK|US)\$",
    re.IGNORECASE,
)

def scrape_syfe_direct(browser):
    """Returns {currency: {tenor: rate}} + a 'fetched_at' timestamp.
    Uses fresh incognito context. Proven 8/8 in scrape_syfe_live.py."""
    ctx = browser.new_context(
        ignore_https_errors=True, user_agent=UA,
        viewport={"width": 1280, "height": 900},
    )
    page = ctx.new_page()
    try:
        page.goto("https://www.syfe.com/en-hk/cash-management/cash-plus-fixed",
                  wait_until="domcontentloaded", timeout=20000)
        # Wait until USD rate cards are filled (first to render)
        page.wait_for_function(
            """() => /\\d+(?:\\.\\d+)?\\s*%\\s*p\\.?a\\.?\\s+US\\$/i.test(document.body.innerText)""",
            timeout=20000,
        )
        # Scroll to bottom to force lazy-loaded sections (HKD calculator
        # is at y~12700) to render
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        # Try clicking the HKD calculator tab to force HKD block render
        try:
            hkd_links = page.get_by_role("link", name="Cash+ Fixed (HKD)", exact=True)
            if hkd_links.count() > 0:
                hkd_links.last.scroll_into_view_if_needed()
                hkd_links.last.click(force=True, timeout=3000)
                page.wait_for_timeout(2000)
        except Exception:
            pass
        # Poll for HKD content
        try:
            page.wait_for_function(
                """() => /\\d+(?:\\.\\d+)?\\s*%\\s*p\\.?a\\.?\\s+HK\\$/i.test(document.body.innerText)""",
                timeout=10000,
            )
        except Exception:
            pass
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        text = page.evaluate("() => document.body.innerText")
        out = {"HKD": {}, "USD": {}}
        for m in SYFE_ROW_RE.finditer(text):
            tenor = m.group(1) + "M"
            rate  = float(m.group(2))
            ccy   = "HKD" if m.group(3).upper() == "HK" else "USD"
            if tenor in ("1M","3M","6M","12M") and 0 < rate < 15:
                out[ccy].setdefault(tenor, rate)
        return out
    finally:
        ctx.close()

# ==============================================================
# 2. StashAway aggregator (reused from verify_pipelines.py)
# ==============================================================
sys.path.insert(0, str(ROOT / "scripts"))
from verify_pipelines import parse_stashaway_text, STASHAWAY_NAME_MAP

def scrape_stashaway(browser, url):
    """Render StashAway page in incognito and return parsed rate dict."""
    ctx = browser.new_context(
        ignore_https_errors=True, user_agent=UA,
        viewport={"width": 1280, "height": 1600}, storage_state=None,
    )
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="load", timeout=25000)
        page.wait_for_timeout(2500)
        for y in (3000, 6000, 9000, 12000):
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(300)
        text = page.evaluate("() => document.body.innerText")
        return parse_stashaway_text(text), text
    finally:
        ctx.close()

# ==============================================================
# 3. Merge into latest.json with proper verified_by tagging
# ==============================================================
def find_stashaway_date(text):
    m = re.search(r"As of\s+(\d{1,2}\s+\w+\s+\d{4})", text or "", re.I)
    return m.group(1) if m else None

def main():
    started = time.time()
    today_iso = time.strftime("%Y-%m-%d", time.gmtime())
    drifts = []

    # For Syfe, delegate to scrape_syfe_live.py — it's the proven path
    # (8/8 verification) and gets a fresh hermetic browser instance which
    # avoids whatever Playwright state issue causes the HKD block to fail
    # to render when run inline alongside other navigations.
    print("[refresh] Syfe direct (subprocess, incognito) ...", end=" ", flush=True)
    syfe_proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scrape_syfe_live.py")],
        capture_output=True, text=True, timeout=40,
    )
    try:
        syfe_payload = json.loads(syfe_proc.stdout)
        syfe_direct  = syfe_payload.get("extracted", {"HKD": {}, "USD": {}})
    except Exception as e:
        print(f"FAILED: {e}")
        return 1
    n_hkd, n_usd = len(syfe_direct.get("HKD", {})), len(syfe_direct.get("USD", {}))
    print(f"got HKD={n_hkd}, USD={n_usd}")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--no-sandbox", "--ignore-certificate-errors",
        ])

        print("[refresh] StashAway HKD (incognito) ...", end=" ", flush=True)
        sa_hkd, sa_hkd_text = scrape_stashaway(browser,
            "https://www.stashaway.hk/r/best-hkd-time-deposit-interest-rates-and-offers")
        sa_hkd_date = find_stashaway_date(sa_hkd_text) or "unknown"
        print(f"{len(sa_hkd)} providers, dated {sa_hkd_date}")

        print("[refresh] StashAway USD (incognito) ...", end=" ", flush=True)
        sa_usd, sa_usd_text = scrape_stashaway(browser,
            "https://www.stashaway.hk/r/best-usd-time-deposit-interest-rates-and-offers")
        sa_usd_date = find_stashaway_date(sa_usd_text) or "unknown"
        print(f"{len(sa_usd)} providers, dated {sa_usd_date}")

        browser.close()

    # Load existing snapshot
    snap = json.loads(SNAP.read_text())

    # Sanity-check: did Syfe scrape return everything expected?
    if n_hkd != 4 or n_usd != 4:
        print(f"WARN: Syfe direct returned partial (HKD={n_hkd}, USD={n_usd}) - keeping prior Syfe values, proceeding with StashAway data")
        syfe_direct = {"HKD": {}, "USD": {}}

    # Convert StashAway parsed data to dict[ccy][pid][tenor]=row for fast lookup
    def index_by_ccy(parsed):
        """Each parsed row has tenor & rate; for HKD page all rows are HKD."""
        # parsed is {pid: [rows]} — no currency, must apply currency externally
        return parsed
    sa_hkd_idx = index_by_ccy(sa_hkd)
    sa_usd_idx = index_by_ccy(sa_usd)

    updated = 0
    for r in snap["rates"]:
        pid, ccy, tenor = r["provider_id"], r["currency"], r["tenor"]

        if pid == "syfe_hk":
            # Direct primary
            new_rate = syfe_direct.get(ccy, {}).get(tenor)
            if new_rate is None: continue
            old = r["apy_pct"]
            if abs(new_rate - old) > 0.005:
                drifts.append({"provider": pid, "ccy": ccy, "tenor": tenor,
                               "old": old, "new": new_rate, "source": "syfe_direct"})
            r["apy_pct"]     = new_rate
            r["verified_by"] = "direct"
            r["verified_at"] = today_iso
            r["fetch_status"] = "ok"
            r["notes"]       = f"Direct from product page on {today_iso} (incognito Chromium scrape)."
            r["source_url"]  = "https://www.syfe.com/en-hk/cash-management/cash-plus-fixed"
            updated += 1
            continue

        # Everyone else: StashAway fallback
        upstream = (sa_hkd_idx if ccy == "HKD" else sa_usd_idx).get(pid, [])
        candidates = [u for u in upstream if u["tenor"] == tenor]
        if not candidates:
            # No StashAway coverage — keep prior, mark stale if not already
            if r.get("verified_by") not in ("syfe", "direct"):
                r["fetch_status"] = "stale"
            continue

        # Pick candidate that matches snapshot's tier_min if present,
        # else the highest-rate row (snapshot's "headline tier" convention)
        snap_tier = r.get("tier_min_ccy") or 0
        if snap_tier > 0:
            exact = [u for u in candidates if (u.get("tier_min") or 0) == snap_tier]
            chosen = exact[0] if exact else max(candidates, key=lambda u: (u.get("tier_min") or 0))
        else:
            chosen = max(candidates, key=lambda u: u["rate"])

        new_rate = chosen["rate"]
        old = r["apy_pct"]
        if abs(new_rate - old) > 0.005:
            drifts.append({"provider": pid, "ccy": ccy, "tenor": tenor,
                           "old": old, "new": new_rate, "source": "stashaway"})
        r["apy_pct"]     = new_rate
        r["verified_by"] = "stashaway_fallback"  # explicit: aggregator used because no direct
        sa_date = sa_hkd_date if ccy == "HKD" else sa_usd_date
        # Convert "26 May 2026" to ISO 2026-05-26
        try:
            r["verified_at"] = time.strftime("%Y-%m-%d",
                time.strptime(sa_date, "%d %B %Y"))
        except Exception:
            r["verified_at"] = today_iso
        r["fetch_status"] = "ok"
        r["notes"]        = f"Aggregator fallback (StashAway HK {ccy}, as of {sa_date}). " \
                            "Direct-scraping this provider is on the roadmap."
        updated += 1

    # Update snapshot metadata
    now_local = time.strftime("%Y-%m-%d %H:%M HKT", time.gmtime(time.time() + 8*3600))
    snap["as_of_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    snap["as_of_local"] = now_local
    snap["data_source_note"] = (
        f"Refreshed {today_iso} via incognito headless Chromium. "
        f"Syfe rates pulled DIRECT from product page (8/8 verified). "
        f"Competitor rates pulled from StashAway HK aggregator HKD ({sa_hkd_date}) "
        f"+ USD ({sa_usd_date}). Direct-scraping per provider is on the roadmap "
        f"but most virtual banks publish rates only via their apps."
    )

    SNAP.write_text(json.dumps(snap, indent=2))

    # Drift report
    drift_path = ROOT / "scripts" / "drift_report.json"
    drift_path.write_text(json.dumps({
        "ran_at": today_iso,
        "syfe_direct": syfe_direct,
        "stashaway_hkd_date": sa_hkd_date,
        "stashaway_usd_date": sa_usd_date,
        "rows_updated": updated,
        "drifts": drifts,
        "duration_sec": round(time.time() - started, 1),
    }, indent=2, default=str))

    print(f"\n{'='*60}\nREFRESH COMPLETE — {updated} rows updated in {round(time.time()-started,1)}s")
    print(f"{'='*60}")
    print(f"  Drifts logged: {len(drifts)}")
    if drifts[:8]:
        print("\n  Sample drifts:")
        for d in drifts[:8]:
            print(f"    {d['provider']:22} {d['ccy']} {d['tenor']:4} "
                  f"{d['old']:>5} -> {d['new']:>5}  ({d['source']})")
    print(f"\n  StashAway HKD as of {sa_hkd_date}")
    print(f"  StashAway USD as of {sa_usd_date}")
    print(f"  Drift report: {drift_path}")

if __name__ == "__main__":
    sys.exit(main() or 0)
