"""
Production refresh runner — Firecrawl-backed.

This is the entry point for the SCHEDULED REFRESH job. Run it daily/weekly
via Cowork's scheduled task system:

    mcp__scheduled-tasks__create_scheduled_task(
      cronExpression="0 9 * * *",           # 9am HKT daily
      prompt="Run python3 verification/refresh_firecrawl.py"
    )

Why Firecrawl over local Playwright:
  - JS rendering: handled natively, no Chromium binaries to maintain.
  - Anti-bot: rotating proxies + Cloudflare bypass built in.
  - Structured extraction: pass a JSON schema, get typed rows back —
    no fragile regex that breaks on a page redesign.
  - Idempotent: same call within cacheTtl returns cached result, free.

Requirements:
  - FIRECRAWL_API_KEY env var (Cowork can pass via the scheduled task)
  - pip install firecrawl-py   (the official SDK)

Output:
  - hk-td-rates/data/latest.json   (snapshot)
  - hk-td-rates/data/history/<ts>.json   (append-only audit trail)
  - verification/firecrawl_report.json   (per-call timings, statuses)
  - verification/drift_report.json       (rate movement since last run)

The script does NOT abort on individual-provider failure: each source has
its own try/except and contributes whatever rows it returned. The aggregate
metric is "what fraction of (provider × currency × tenor) cells did we
successfully refresh" — surfaced in the report.
"""
from __future__ import annotations
import json, os, pathlib, sys, time, re
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT
SNAP_PATH = SKILL_ROOT / "data" / "latest.json"
HIST_DIR  = SKILL_ROOT / "data" / "history"
HIST_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# Dependency check + graceful degradation
# ============================================================
try:
    from firecrawl import FirecrawlApp           # firecrawl-py >= 1.0
except ImportError:
    FirecrawlApp = None

API_KEY = os.environ.get("FIRECRAWL_API_KEY")

# ============================================================
# Load provider catalog from YAML
# ============================================================
try:
    import yaml
except ImportError:
    print("[refresh_firecrawl] pip install pyyaml", file=sys.stderr)
    sys.exit(2)

YAML_PATH = SKILL_ROOT / "providers.yaml"
catalog = yaml.safe_load(YAML_PATH.read_text())

FIRECRAWL_GLOBAL = catalog.get("firecrawl", {})
DEFAULT_OPTIONS  = FIRECRAWL_GLOBAL.get("default_options", {})
DEFAULT_SCHEMA   = FIRECRAWL_GLOBAL.get("default_schema", {})

# ============================================================
# Firecrawl extractor
# ============================================================
def firecrawl_extract(app, url: str, source_cfg: dict) -> dict:
    """Call Firecrawl /extract for one URL and return parsed rows.

    Merges global default_options with per-source firecrawl_config overrides.
    """
    cfg     = source_cfg.get("firecrawl_config", {}) or {}
    schema  = cfg.get("schema") or DEFAULT_SCHEMA
    options = {**DEFAULT_OPTIONS, **{k: v for k, v in cfg.items() if k != "schema"}}

    # Firecrawl Python SDK signature:
    #   app.extract([url], schema=..., **kwargs)
    # We use the structured-extract path that returns JSON matching the schema.
    started = time.time()
    try:
        result = app.extract(
            [url],
            schema=schema,
            **options,
        )
        ok = bool(result and (result.get("data") or result.get("success")))
        return {
            "ok":       ok,
            "data":     result.get("data") if isinstance(result, dict) else result,
            "raw":      result,
            "latency":  round(time.time() - started, 2),
            "error":    None,
        }
    except Exception as e:
        return {
            "ok":      False, "data": None, "raw": None,
            "latency": round(time.time() - started, 2),
            "error":   str(e)[:300],
        }


def normalize_rows(extracted: dict, source: dict) -> list[dict]:
    """Turn Firecrawl's schema-shaped output into rate rows that match
    our snapshot's format."""
    if not extracted: return []
    rows_in = extracted.get("rows", []) or []
    out = []
    for r in rows_in:
        tenor_int = r.get("tenor_months")
        if tenor_int not in (1, 3, 6, 12): continue
        rate = r.get("rate_pct")
        if rate is None or not (0 <= float(rate) <= 15): continue
        out.append({
            "currency":         r.get("currency", "").upper(),
            "tenor":            f"{int(tenor_int)}M",
            "apy_pct":          round(float(rate), 4),
            "tier_min_ccy":     r.get("min_deposit_amount"),
            "promo":            bool(r.get("new_funds_only") or r.get("online_channel_only")),
            "new_money_only":   bool(r.get("new_funds_only")),
            "online_only":      bool(r.get("online_channel_only")),
            "promo_end_date":   r.get("promo_end_date"),
            "sub_product":      r.get("sub_product"),
            "raw_provider_name": r.get("provider_name"),
        })
    return out

# ============================================================
# Aggregator (StashAway) fallback path
# ============================================================
def firecrawl_aggregator_fallback(app, aggregators: list[dict]) -> dict:
    """Hit each aggregator with the same schema, merge results indexed
    by (provider_id, currency, tenor)."""
    merged = {}     # (pid, ccy, tenor) -> {row}
    for agg in aggregators:
        for src in agg.get("sources", []):
            res = firecrawl_extract(app, src["url"], src)
            if not res["ok"]: continue
            rows = normalize_rows(res["data"], src)
            for r in rows:
                # Aggregator rows usually carry a raw_provider_name like
                # "Fubon Bank" — we keep them under a special "_aggregator"
                # provider_id namespace and let the merge step apply name
                # → pid translation.
                key = (r["raw_provider_name"], r["currency"], r["tenor"])
                if key not in merged:
                    merged[key] = r
    return merged

# ============================================================
# Merge direct + aggregator into the snapshot
# ============================================================
NAME_TO_PID = {
    # Same mapping used in verify_pipelines.py — keep these in sync
    "HSBC Premier": "hsbc",  "HSBC One": "hsbc",
    "Bank of China": "bochk",
    "Standard Chartered": "standard_chartered",
    "ICBC Asia": "icbc_asia",
    "China CITIC Bank": "china_citic",
    "Fubon Bank": "fubon_hk",
    "Bank of Communications": "bocom_hk",
    "Bank of East Asia": "bea",
    "Hang Seng Bank": "hang_seng",
    "Chong Hing Bank": "chong_hing",
    "Nanyang Commercial Bank": "nanyang_commercial",
    "CMB Wing Lung Bank": "cmb_wing_lung",
    "Public Bank": "public_bank_hk",
    "Public Bank (Hong Kong)": "public_bank_hk",
    "Shanghai Commercial Bank": "shanghai_commercial",
    "Fusion Bank": "fusion",
    "WeLab Bank GoSave 2.0": "welab",
    "WeLab Bank": "welab",
    "AirStar Bank": "airstar",
    "ZA Bank": "za_bank",
    "Mox Bank": "mox",
    "Livi Bank": "livi", "livi Bank": "livi",
    "CCB Asia": "ccb_asia",
    "DBS Bank": "dbs_hk",
    "OCBC Hong Kong": "ocbc_hk",
}


def main():
    started = time.time()
    today_iso = time.strftime("%Y-%m-%d", time.gmtime())

    if FirecrawlApp is None:
        print("[refresh_firecrawl] firecrawl-py not installed.")
        print("[refresh_firecrawl] Falling back to local Playwright path (refresh_rates.py).")
        os.execv(sys.executable, [sys.executable, str(ROOT / "scripts" / "refresh_rates.py")])

    if not API_KEY:
        print("[refresh_firecrawl] FIRECRAWL_API_KEY env var not set.")
        print("[refresh_firecrawl] Falling back to local Playwright path (refresh_rates.py).")
        os.execv(sys.executable, [sys.executable, str(ROOT / "scripts" / "refresh_rates.py")])

    app = FirecrawlApp(api_key=API_KEY)

    # ---- 1. Direct: each provider in catalog.providers ----
    direct_rows = {}   # (pid, ccy, tenor) -> row
    direct_diag = []
    for prov in catalog.get("providers", []):
        pid = prov["id"]
        for src in prov.get("sources", []):
            res = firecrawl_extract(app, src["url"], src)
            direct_diag.append({
                "provider_id": pid, "url": src["url"],
                "ok": res["ok"], "latency": res["latency"], "error": res["error"],
            })
            if not res["ok"]: continue
            rows = normalize_rows(res["data"], src)
            for r in rows:
                # Direct fetch: trust provider_id of the catalog entry
                if r["currency"] not in ("HKD", "USD"): continue
                key = (pid, r["currency"], r["tenor"])
                # Prefer rows where sub_product matches what we want
                # (e.g. snapshot has "hsbc" + sub_product "HSBC One")
                if key not in direct_rows:
                    direct_rows[key] = {**r, "verified_by": "direct", "source_url": src["url"]}

    # ---- 2. Aggregator: StashAway HKD + USD ----
    agg_rows = firecrawl_aggregator_fallback(app, catalog.get("aggregators", []))
    # Convert agg keys to (pid, ccy, tenor) using NAME_TO_PID
    agg_indexed = {}
    for (raw_name, ccy, tenor), row in agg_rows.items():
        pid = NAME_TO_PID.get(raw_name or "")
        if not pid: continue
        key = (pid, ccy, tenor)
        if key not in agg_indexed or (row.get("tier_min_ccy") or 0) > (agg_indexed[key].get("tier_min_ccy") or 0):
            agg_indexed[key] = {**row, "verified_by": "stashaway_fallback"}

    # ---- 3. Merge into snapshot: direct wins, agg fallback ----
    snap = json.loads(SNAP_PATH.read_text())
    drifts = []
    refreshed = 0
    for r in snap["rates"]:
        key = (r["provider_id"], r["currency"], r["tenor"])
        chosen = direct_rows.get(key) or agg_indexed.get(key)
        if not chosen:
            # Keep prior, flag stale
            r["fetch_status"] = "stale"
            continue

        old_rate = r["apy_pct"]
        new_rate = chosen["apy_pct"]
        if abs(new_rate - old_rate) > 0.005:
            drifts.append({"key": list(key), "old": old_rate, "new": new_rate,
                           "source": chosen["verified_by"]})
        r["apy_pct"]      = new_rate
        r["verified_by"]  = chosen["verified_by"]
        r["verified_at"]  = today_iso
        r["fetch_status"] = "ok"
        r["source_url"]   = chosen.get("source_url", r.get("source_url"))
        refreshed += 1

    # Snapshot metadata
    now = time.gmtime()
    snap["as_of_utc"]   = time.strftime("%Y-%m-%dT%H:%M:%SZ", now)
    snap["as_of_local"] = time.strftime("%Y-%m-%d %H:%M HKT", time.gmtime(time.time() + 8*3600))
    snap["data_source_note"] = (
        f"Refreshed {today_iso} via Firecrawl. "
        f"Direct provider scrapes verified {sum(1 for r in snap['rates'] if r.get('verified_by')=='direct')} rows; "
        f"StashAway fallback covered {sum(1 for r in snap['rates'] if r.get('verified_by')=='stashaway_fallback')} rows. "
        f"Run id: firecrawl-{int(time.time())}."
    )
    SNAP_PATH.write_text(json.dumps(snap, indent=2))

    # Append-only history
    ts = time.strftime("%Y-%m-%d-%H%M", now)
    (HIST_DIR / f"{ts}.json").write_text(json.dumps(snap, indent=2))

    # Reports
    rpt = {
        "ran_at_utc":      time.strftime("%Y-%m-%dT%H:%M:%SZ", now),
        "duration_sec":    round(time.time() - started, 1),
        "rows_refreshed":  refreshed,
        "drifts":          drifts,
        "direct_calls":    direct_diag,
        "aggregator_keys": len(agg_indexed),
    }
    (ROOT / "scripts" / "firecrawl_report.json").write_text(
        json.dumps(rpt, indent=2, default=str))

    print(f"[refresh_firecrawl] done in {rpt['duration_sec']}s — "
          f"{refreshed} rows refreshed, {len(drifts)} drifts logged.")

if __name__ == "__main__":
    main()
