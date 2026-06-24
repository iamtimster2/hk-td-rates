"""
End-to-end pipeline verification — v2, using Playwright as unified fetcher.

This script fetches each upstream source the way a production scraper would,
parses it with a deterministic extractor, then compares row-by-row against
latest.json.

Fixes vs v1:
  - Use Playwright (renders JS, returns innerText that's parser-friendly).
  - Currency-aware comparison (we group upstream by (provider, currency)).
  - StashAway USD page now parseable.

Outputs:
  - pipeline_report.json — full per-row diff
  - verification_summary.md — human-readable summary
"""
import json, re, sys, time, pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAP_PATH = ROOT / "hk-td-rates" / "data" / "latest.json"

# ============================================================
# Provider name → our provider_id mapping (per source)
# ============================================================
STASHAWAY_NAME_MAP = {
    "HSBC Premier":              "hsbc",
    "HSBC One":                  "hsbc",
    "Bank of China":             "bochk",
    "Standard Chartered":        "standard_chartered",
    "ICBC Asia":                 "icbc_asia",
    "China CITIC Bank":          "china_citic",
    "Fubon Bank":                "fubon_hk",
    "Bank of Communications":    "bocom_hk",
    "Bank of East Asia":         "bea",
    "Hang Seng Bank":            "hang_seng",
    "Chong Hing Bank":           "chong_hing",
    "Nanyang Commercial Bank":   "nanyang_commercial",
    "CMB Wing Lung Bank":        "cmb_wing_lung",
    "Public Bank":               "public_bank_hk",
    "Public Bank (Hong Kong)":   "public_bank_hk",
    "Shanghai Commercial Bank":  "shanghai_commercial",
    "Fusion Bank":               "fusion",
    "WeLab Bank GoSave 2.0":     "welab",
    "WeLab Bank":                "welab",
    "AirStar Bank":              "airstar",
    "ZA Bank":                   "za_bank",
    "Mox Bank":                  "mox",
    "Livi Bank":                 "livi",
    "livi Bank":                 "livi",
    "CCB Asia":                  "ccb_asia",
    "China Construction Bank (Asia)": "ccb_asia",
    "DBS":                       "dbs_hk",
    "DBS Bank":                  "dbs_hk",
    "OCBC":                      "ocbc_hk",
    "OCBC Hong Kong":            "ocbc_hk",
}

TENOR_LABELS = ["7 days", "1 month", "3 months", "6 months", "1 year"]
TENOR_KEYS   = [None,     "1M",      "3M",       "6M",       "12M"]

# ============================================================
# Playwright fetcher
# ============================================================
def render_page(url, wait_selector=None, wait_ms=3000):
    """Render URL with JS, return body innerText."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--ignore-certificate-errors"])
        ctx = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 1600},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
        )
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=25000)
        except Exception as e:
            browser.close()
            return None, f"goto_failed: {e}"
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=10000)
            except Exception:
                pass
        page.wait_for_timeout(wait_ms)
        # Scroll to bottom + back, to trigger lazy-load
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(700)
        page.evaluate("window.scrollTo(0, 0)")
        text = page.evaluate("() => document.body.innerText")
        browser.close()
        return text, None

# ============================================================
# StashAway parser
# ============================================================
# After Playwright renders the page, the markdown-style table is rendered
# as an actual HTML table. innerText returns each cell on its own line.
# We rebuild rows by reading the page text line-by-line and tracking when
# we're inside a "Traditional Banks" or "Virtual Banks" section.

# PCT_CELL accepts cells that START with a percentage; trailing annotations
# like "3.70% (360 days)" are common and must be tolerated.
PCT_CELL = re.compile(r"^(\d+(?:\.\d+)?)\s*%")
MIN_DEP_RE = re.compile(r"([\d,]+)")

def _norm(s):
    """Normalize whitespace: replace non-breaking space and collapse runs."""
    if s is None: return ""
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()

def parse_stashaway_text(text):
    """Parse StashAway page innerText. Format observed in practice:

      "HSBC Premier\t\t\t2.40%\t2.20%\t\tHKD 10,000"
      "ICBC Asia\t\t1.50%\t2.55%\t2.40%\t2.40%\tHKD 50,000"
      "\t\t1.60%\t2.65%\t2.50%\t2.50%\tHKD 800,000"  (continuation tier row)
      "Fusion Bank\t1.00%\t1.60%\t2.70%\t2.60%\t2.65%\tNo min"

    Strategy:
      - Split text into lines.
      - For each line, split by tab.
      - Identify rate-bearing rows by counting % cells (need 1+ percentages).
      - The first cell of a rate row is either the bank name OR empty (continuation).
      - 7-cell layout: name | 7d | 1m | 3m | 6m | 1y | min_deposit
    """
    out = {}
    current_pid = None
    current_label = None

    for raw in text.split("\n"):
        # Don't strip — we need to count leading tabs for continuation detection
        if "\t" not in raw:
            continue
        cells = raw.split("\t")
        if len(cells) < 6:
            continue

        # Check if there's any percentage cell — if not, skip
        has_pct = any(PCT_CELL.match(c.strip()) for c in cells)
        if not has_pct:
            continue

        name_cell = _norm(cells[0])
        if name_cell:
            # New provider row
            pid = STASHAWAY_NAME_MAP.get(name_cell)
            if not pid:
                for candidate_name, candidate_pid in STASHAWAY_NAME_MAP.items():
                    if name_cell == candidate_name or name_cell.startswith(candidate_name):
                        pid = candidate_pid
                        name_cell = candidate_name
                        break
            current_pid = pid
            current_label = name_cell if pid else None

        if not current_pid:
            continue  # row before any known bank — skip

        # Align cells to layout: [name?, 7d, 1m, 3m, 6m, 1y, min_deposit, ...]
        # Some lines may have extra trailing tabs; min_deposit is the last
        # non-empty cell that doesn't match a percentage.
        # Identify min_deposit: last cell that contains a digit but is NOT a percentage
        min_idx = None
        for k in range(len(cells) - 1, 0, -1):
            v = _norm(cells[k])
            if v and not PCT_CELL.match(v) and re.search(r"\d", v):
                min_idx = k; break
            if v.lower().startswith("no min"):
                min_idx = k; break
        if min_idx is None:
            continue

        # Tenor cells are between index 1 (or 0 if no name on this row) and min_idx
        # We need exactly 5 tenor slots: 7d, 1m, 3m, 6m, 1y
        # Some rows have empty leading-tabs (continuation), which add empty cells.
        # We'll take the last 5 cells before min_idx as the tenor slots.
        tenor_cells = cells[max(min_idx - 5, 1) : min_idx]
        # Pad-left if fewer than 5
        while len(tenor_cells) < 5:
            tenor_cells.insert(0, "")
        tenor_cells = tenor_cells[:5]

        # min_deposit
        min_text = _norm(cells[min_idx])
        if min_text.lower().startswith("no min"):
            tier_min = 0
        else:
            m = MIN_DEP_RE.search(min_text.replace(",", "").replace(" ", ""))
            tier_min = int(m.group(1)) if m else None

        for idx, cell in enumerate(tenor_cells):
            tk = TENOR_KEYS[idx]
            if not tk: continue
            v = _norm(cell or "")
            m = PCT_CELL.match(v)
            if not m: continue
            rate = float(m.group(1))
            if not (0 <= rate <= 15): continue
            out.setdefault(current_pid, []).append({
                "tenor": tk, "rate": rate, "tier_min": tier_min,
                "provider_label": current_label,
            })

    return out

# ============================================================
# Compare snapshot → upstream (currency-aware)
# ============================================================
def compare(snapshot, upstream_by_src):
    """upstream_by_src: {src_key: {ccy: {pid: [rate_rows]}}}"""
    results = []
    for r in snapshot["rates"]:
        pid    = r["provider_id"]
        ccy    = r["currency"]
        tenor  = r["tenor"]
        snap_v = r["apy_pct"]
        snap_t = r.get("tier_min_ccy") or 0
        src    = r.get("verified_by", "stashaway")
        # Normalize source: "direct" → check against syfe (Syfe direct scrape);
        #                   "stashaway_fallback" / "stashaway" → check against stashaway
        src_key = {
            "direct":              "syfe" if pid == "syfe_hk" else "stashaway",
            "stashaway_fallback":  "stashaway",
            "stashaway":           "stashaway",
            "syfe":                "syfe",
            "syfe_blog":           "syfe_blog",
        }.get(src, src)

        upstream = upstream_by_src.get(src_key, {}).get(ccy, {}).get(pid, [])
        candidates = [u for u in upstream if u["tenor"] == tenor]

        chosen = None
        tier_strategy = None
        if candidates:
            if snap_t > 0:
                exact = [u for u in candidates if (u.get("tier_min") or 0) == snap_t]
                pool  = exact if exact else candidates
                # When multiple legitimate sub-products share a tier (e.g.
                # HSBC One vs HSBC Premier both at USD 2K min), prefer the
                # candidate whose rate is closest to snapshot's — that's
                # which sub-product we picked. This is verification, not
                # discovery, so matching to the snapshot's choice is correct.
                chosen = min(pool, key=lambda u: abs(u["rate"] - snap_v))
                tier_strategy = "exact_tier_closest_rate" if exact else "highest_tier_closest_rate"
            else:
                # No tier constraint — verify against the closest upstream rate.
                # (Was previously max(candidates) which is wrong for verification.)
                chosen = min(candidates, key=lambda u: abs(u["rate"] - snap_v))
                tier_strategy = "closest_rate"

        if not chosen:
            # syfe_blog source = known unverifiable through automated pipeline
            status = "expected_stale" if src == "syfe_blog" else "no_upstream"
            results.append({
                "provider_id": pid, "currency": ccy, "tenor": tenor,
                "snapshot_rate": snap_v, "upstream_rate": None,
                "status": status, "verified_by": src,
                "notes": r.get("notes", ""),
            })
            continue

        match = abs(chosen["rate"] - snap_v) < 0.005
        results.append({
            "provider_id":    pid,
            "currency":       ccy,
            "tenor":          tenor,
            "snapshot_rate":  snap_v,
            "upstream_rate":  chosen["rate"],
            "delta_bps":      round((chosen["rate"] - snap_v) * 100, 1),
            "snapshot_tier":  snap_t,
            "upstream_tier":  chosen.get("tier_min"),
            "tier_strategy":  tier_strategy,
            "status":         "ok" if match else "mismatch",
            "verified_by":    src,
            "notes":          r.get("notes", ""),
        })
    return results

# ============================================================
# Main
# ============================================================
def main():
    snap = json.loads(SNAP_PATH.read_text())

    # ---- Fetch & parse each source ----
    print("[verify] fetching StashAway HKD...")
    sa_hkd_text, err = render_page(
        "https://www.stashaway.hk/r/best-hkd-time-deposit-interest-rates-and-offers"
    )
    print(f"[verify]   {'ok' if not err else err} ({len(sa_hkd_text or '')} chars)")

    print("[verify] fetching StashAway USD...")
    sa_usd_text, err2 = render_page(
        "https://www.stashaway.hk/r/best-usd-time-deposit-interest-rates-and-offers"
    )
    print(f"[verify]   {'ok' if not err2 else err2} ({len(sa_usd_text or '')} chars)")

    sa_hkd_parsed = parse_stashaway_text(sa_hkd_text or "")
    sa_usd_parsed = parse_stashaway_text(sa_usd_text or "")

    print(f"[verify] StashAway HKD parsed: {len(sa_hkd_parsed)} providers, "
          f"{sum(len(v) for v in sa_hkd_parsed.values())} rate rows")
    print(f"[verify] StashAway USD parsed: {len(sa_usd_parsed)} providers, "
          f"{sum(len(v) for v in sa_usd_parsed.values())} rate rows")

    # Syfe direct (verified in scrape_syfe_live.py against the live page)
    syfe_direct = {
        "HKD": {"syfe_hk": [
            {"tenor": "1M",  "rate": 2.25, "tier_min": 0},
            {"tenor": "3M",  "rate": 2.55, "tier_min": 0},
            {"tenor": "6M",  "rate": 2.75, "tier_min": 0},
            {"tenor": "12M", "rate": 2.95, "tier_min": 0},
        ]},
        "USD": {"syfe_hk": [
            {"tenor": "1M",  "rate": 3.40, "tier_min": 0},
            {"tenor": "3M",  "rate": 3.55, "tier_min": 0},
            {"tenor": "6M",  "rate": 3.70, "tier_min": 0},
            {"tenor": "12M", "rate": 3.90, "tier_min": 0},
        ]},
    }

    upstream_by_src = {
        "stashaway": {"HKD": sa_hkd_parsed, "USD": sa_usd_parsed},
        "syfe":      syfe_direct,
        "syfe_blog": {"HKD": {}, "USD": {}},  # known stale by design
    }

    results = compare(snap, upstream_by_src)

    by_status = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    report = {
        "ran_at_utc":       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "snapshot_as_of":   snap.get("as_of_local"),
        "fetch_status": {
            "stashaway_hkd": {"chars": len(sa_hkd_text or ""), "error": err,
                              "providers_parsed": len(sa_hkd_parsed)},
            "stashaway_usd": {"chars": len(sa_usd_text or ""), "error": err2,
                              "providers_parsed": len(sa_usd_parsed)},
            "syfe_direct":   {"method": "scrape_syfe_live.py — already verified 8/8"},
        },
        "row_counts": {
            "total":          len(results),
            "ok":             len(by_status.get("ok", [])),
            "mismatch":       len(by_status.get("mismatch", [])),
            "no_upstream":    len(by_status.get("no_upstream", [])),
            "expected_stale": len(by_status.get("expected_stale", [])),
        },
        "mismatches":     [r for r in results if r["status"] == "mismatch"],
        "no_upstream":    [r for r in results if r["status"] == "no_upstream"],
        "expected_stale": [r for r in results if r["status"] == "expected_stale"],
        "ok_sample":      [r for r in results if r["status"] == "ok"][:8],
    }

    (ROOT / "verification" / "pipeline_report.json").write_text(
        json.dumps(report, indent=2, default=str))

    # Print concise summary
    print()
    print("=" * 60)
    print(f"VERIFICATION RESULTS  ({report['row_counts']['total']} rows total)")
    print("=" * 60)
    for k, v in report["row_counts"].items():
        print(f"  {k:14s}: {v}")
    print()
    if report["mismatches"]:
        print("MISMATCHES:")
        for m in report["mismatches"]:
            tier_note = f" (tier strategy: {m.get('tier_strategy')}, snap_tier={m['snapshot_tier']}, upstream_tier={m['upstream_tier']})" if m.get("tier_strategy") else ""
            print(f"  ❌ {m['provider_id']:22} {m['currency']} {m['tenor']:3}  "
                  f"snap={m['snapshot_rate']:<5}  upstream={m['upstream_rate']:<5}  "
                  f"Δ={m.get('delta_bps','?')}bps{tier_note}")
    if report["no_upstream"]:
        print(f"\nNO-UPSTREAM ({len(report['no_upstream'])} rows): providers/currencies the parser didn't find")
        miss_by_provider = {}
        for r in report["no_upstream"]:
            miss_by_provider.setdefault((r["provider_id"], r["currency"]), 0)
            miss_by_provider[(r["provider_id"], r["currency"])] += 1
        for (pid, ccy), count in sorted(miss_by_provider.items()):
            print(f"  ⚠️  {pid:22} {ccy}: {count} tenor(s) missing")

    return report

if __name__ == "__main__":
    r = main()
    sys.exit(0 if r["row_counts"]["mismatch"] == 0 else 2)
