---
name: hk-td-rates
description: |
  Pull current Hong Kong time deposit rates from all in-scope providers
  (traditional banks, virtual banks, wealth platforms), normalize them into
  a single snapshot, and render a client-facing comparison artifact that
  positions Syfe against the field. Use when the user asks to "refresh HK
  TD rates", "pull TD rates", "compare our HK rates", or anything similar.
---

# hk-td-rates — Hong Kong Time Deposit rate puller

This skill turns the manual "open 20 tabs and write down the rates" job into
one on-demand run. It produces:

1. **`data/latest.json`** — normalized snapshot (internal source of truth).
2. **`data/history/<YYYY-MM-DD-HHMM>.json`** — append-only history so trends
   can be reconstructed later.
3. **A live artifact** — Syfe vs. competitors, HKD + USD, 1M / 3M / 6M / 12M,
   with as-of timestamp and source citations. Created via
   `mcp__cowork__create_artifact` so the user can re-open it any time.

The skill is **on-demand**: nothing runs on a schedule unless the user wires
one up separately.

---

## When to trigger

Trigger on requests like:

- "Refresh HK time deposit rates"
- "Pull the latest TD rates in Hong Kong"
- "Update the HK TD comparison"
- "How do our HK rates stack up this week?"

Do **not** trigger for: Singapore TD comparisons, mortgage rates, or savings
account (non-TD) rates unless the user explicitly asks.

---

## Inputs

None required. Optional arguments the user may pass:

- `providers`: subset of provider IDs from `providers.yaml` (e.g.
  `["hsbc","mox","syfe_hk"]`) — useful when one provider's site is down and
  you want a partial refresh.
- `strict_td_only`: boolean, default `false`. When `true`, exclude
  cash-management / MMF products (Futu, Tiger, Endowus) from the artifact
  view. They are still scraped and saved to JSON.

---

## The flow

Follow these steps in order. Mark each as a task via TaskCreate/TaskUpdate
so the user sees progress.

### 1. Load the provider catalog

Read `providers.yaml` from the skill directory. Validate the YAML parses
and every provider has `id`, `name`, `category`, `sources[]`.

### 2. Scrape each provider in parallel — **direct first, aggregator fallback**

The fetch precedence is:

1. **Direct provider page** (primary) — each provider's own rates page
   scraped via **Firecrawl** in production (or headless Chromium for local
   debugging). Firecrawl handles JS rendering, anti-bot challenges, retries,
   and structured extraction natively — it's the right tool for a scheduled
   job that can't afford flaky scrapes. This produces the freshest number
   because the provider updates their own page first; aggregators like
   StashAway lag by 0–7 days. Verified rows get `verified_by: "direct"`
   with today's date.

2. **Aggregator fallback** — if the direct fetch fails (timeout, blocked,
   consent modal we can't dismiss, or page doesn't list that tenor),
   fall through to the StashAway HK aggregator for that
   (provider, currency, tenor) tuple. Verified rows get
   `verified_by: "stashaway_fallback"` with StashAway's "as of" date.

3. **Last-resort Syfe blog** — for providers neither StashAway nor the
   direct page surface (currently: CCB Asia USD, OCBC HK, HSBC USD 12M).
   Tagged `verified_by: "syfe_blog"` and `fetch_status: "stale"` because
   the blog is monthly and known to drift.

### Production scraper: **Firecrawl**

Scheduled refreshes (daily / weekly cron) run through Firecrawl, NOT
through local Playwright. Reasons:

- **JS rendering, baked in.** No need to maintain Chromium binaries, no
  cache flags, no headless-mode quirks (the Syfe HKD render bug from the
  Playwright session would never have happened with Firecrawl).
- **Anti-bot tolerance.** Firecrawl rotates IPs and handles consent
  modals + Cloudflare challenges that block raw fetches.
- **Structured extraction by schema.** Firecrawl's `/extract` endpoint
  takes a JSON schema and returns typed data — no fragile regex. Each
  provider's expected output shape lives in `providers.yaml` under
  `firecrawl_config.schema`.
- **Idempotent + cacheable.** Firecrawl natively supports `cacheTtl`
  so duplicate fetches in the same window are free.
- **One env var.** `FIRECRAWL_API_KEY` — that's all the orchestrator needs.
  No browser binaries to install on the scheduling host.

**Production entry point:** `verification/refresh_firecrawl.py`. It reads
`providers.yaml`, calls Firecrawl's `/extract` for each source, merges
with StashAway fallback, writes `data/latest.json` + a fresh drift report.
Wire it to a scheduled task via Cowork's `mcp__scheduled-tasks__create_scheduled_task`
with a daily cron (`0 9 * * *` HKT).

**Local debugging / verification path** (when Firecrawl is down or you
need to reproduce a scrape locally without burning credits):
- `verification/scrape_syfe_live.py` — direct Playwright scrape of Syfe
- `verification/refresh_rates.py` — Playwright-based full refresh
- `verification/verify_pipelines.py` — cross-check snapshot vs StashAway

These local scripts mirror the production logic and serve as a fallback
if Firecrawl quota is exhausted or the schema changes.

**Always send the scrape requests in a single message with multiple tool
calls** so the providers run concurrently. The wall-clock target for a
full refresh is < 90 seconds.

**For each row, also record:** which sources were tried, which one
returned the rate, and the delta vs other available sources. If direct
and aggregator disagree by > 15 bps, log a `disagreement` note. Drift
data is logged to `verification/pipeline_report.json` and surfaced in the
HTML via a tooltip on the rate cell.

For each row found, record:

```
provider_id, currency, tenor, apy_pct, tier_min_ccy, tier_max_ccy,
promo, new_money_only, online_only, promo_end_date, product_type,
source_url, notes, fetched_at, fetch_status
```

Pin tenors to `1M`, `3M`, `6M`, `12M`. If a provider publishes a
6-month rate as "180 days" or "26 weeks", normalize to `6M` and note
the actual definition in `notes`.

If a provider publishes multiple tier brackets, keep the **headline**
row (the one most prominently displayed — usually the lowest min
ticket) AND the **highest** row, so the artifact can show "best
available" for high-net-worth users.

### 3. Handle failures gracefully

If a provider's source can't be parsed:

- Log it in the `failures[]` array of the snapshot.
- Carry forward the previous value from the most recent
  `data/history/*.json` if one exists, but tag the row with
  `fetch_status: "stale"` and `last_successful_fetch: <iso8601>`.
- Do not abort the whole run — partial data is better than none.

In the artifact, stale rows render with a small "stale" badge and a
tooltip explaining the age.

### 4. Normalize & validate

Run sanity checks on the snapshot before saving:

- All `apy_pct` values are between 0 and 15 (anything outside is almost
  certainly a parse error — flag and exclude).
- Every (provider, currency, tenor) tuple appears at most twice
  (headline + high-tier).
- `as_of_local` is today (HKT). If not, the run is suspect.
- Syfe's row(s) are present in both HKD and USD if Syfe publishes both.
  If missing, raise loudly — the comparison page is meaningless without
  our own number.

### 5. Save the snapshot

Write `data/latest.json` (overwrite) AND
`data/history/<YYYY-MM-DD-HHMM>.json` (append-only).

### 6. Render the artifact

Read `templates/artifact.html`. Inject the snapshot JSON inline
(as a `<script id="snapshot" type="application/json">…</script>` block).
The template handles all rendering client-side from that JSON.

Then create the artifact via `mcp__cowork__create_artifact` with:

- `title`: "HK Time Deposit Rates — Syfe vs. Competitors"
- `html`: the populated template

The artifact embeds the snapshot, so re-opening it shows the same
numbers it was created with. To refresh, the user re-invokes the
skill, which produces a new artifact.

### 7. Summarize in chat

Finish with a brief chat reply:

- As-of timestamp (HKT).
- Headline winners per currency × tenor.
- Where Syfe ranks (e.g. "Syfe USD 3M at 4.50% — #2 of 22, behind Mox
  at 4.65%").
- Any failures (so the user knows what wasn't refreshed).
- Then call `mcp__cowork__present_files` with `data/latest.json` and the
  artifact URL.

---

## Compliance & accuracy guardrails

This skill produces a **client-facing comparison**, so accuracy isn't
optional. Enforce:

- **Every external row must cite a source URL.** The artifact renders
  each competitor cell as a tooltip with the provider's source page.
- **Promo-only rates must be labelled.** If a competitor row is
  "preferential / new-money-only / online-exclusive", show a small
  asterisk and footnote. Never let a promo rate appear as if it's the
  standard board rate.
- **Mark the as-of timestamp prominently** at the top of the artifact,
  in HKT, with hour-precision.
- **Cash-management / MMF products are not TDs.** They get a different
  badge in the artifact and a footnote: "Money market fund — yield
  varies daily, not a fixed-rate time deposit."
- **No predictions.** The artifact shows current published rates only.
  Don't infer trends, project future rates, or compute "after-tax"
  yields unless the user explicitly asks.

If any of these guardrails can't be satisfied for a row, drop the row
rather than ship a misleading number.

---

## Files in this skill

```
hk-td-rates/
├── SKILL.md               ← you are here
├── providers.yaml         ← provider catalog (edit when sites move)
├── templates/
│   └── artifact.html      ← comparison page template (no data)
├── data/
│   ├── latest.json        ← most recent successful snapshot
│   └── history/           ← all historical snapshots
└── scripts/
    └── normalize.py       ← helper for tenor/currency normalization
```

---

## Scheduling the refresh

Run `verification/refresh_firecrawl.py` as a scheduled task. Suggested cadence:
**daily at 9am HKT** (after most banks have published any overnight rate
changes but before the trading day begins in Asia).

To set up via Cowork:

```python
mcp__scheduled-tasks__create_scheduled_task(
    cronExpression="0 9 * * *",
    timezone="Asia/Hong_Kong",
    prompt="Run python3 hk-td-rates/verification/refresh_firecrawl.py "
           "and post a Slack summary if any rate drifted by > 25 bps."
)
```

The scheduled task needs `FIRECRAWL_API_KEY` in its environment. If
unavailable, `refresh_firecrawl.py` automatically degrades to the
Playwright-based `refresh_rates.py` so the snapshot still updates.

After each run, three files change:
1. `data/latest.json` — overwritten with fresh rates
2. `data/history/YYYY-MM-DD-HHMM.json` — append-only audit trail
3. `verification/firecrawl_report.json` — per-call latency, success rate, drifts

## Maintenance notes

- When a provider redesigns their page, only `providers.yaml` changes.
  Update the `url`, flip `method` if needed, and re-run.
- If a new provider enters the market (new HK virtual bank, new wealth
  platform), add a block to `providers.yaml`. The skill picks it up
  automatically on the next run.
- If the artifact layout changes, edit `templates/artifact.html`
  only — never embed data in the template.
- Run a quarterly review of `providers.yaml` against the live HKMA
  registered bank list to catch any banks that exited or merged.
