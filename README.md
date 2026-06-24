# HK Time Deposit Rates

Auto-refreshed comparison of Hong Kong time-deposit rates across 22 providers, with Syfe positioned against the field.

**Live:** [https://USERNAME.github.io/hk-td-rates/](https://USERNAME.github.io/hk-td-rates/) (replace `USERNAME` after setup)

---

## First-time setup — copy/paste these exact commands

You need: a GitHub account, `gh` CLI (or git+browser), and a Firecrawl API key (optional — falls back to Playwright).

### 1. Create the repo and push

```bash
# From the unzipped hk-td-rates folder
cd hk-td-rates

git init
git add .
git commit -m "Initial commit: HK TD rates comparison"
git branch -M main

# Create a public repo on GitHub and push (using gh CLI):
gh repo create hk-td-rates --public --source=. --remote=origin --push

# Or if you don't have gh CLI:
#   1. Create a new public repo at https://github.com/new called "hk-td-rates"
#   2. Then run:
#      git remote add origin https://github.com/YOUR_USERNAME/hk-td-rates.git
#      git push -u origin main
```

### 2. Add the Firecrawl API key as a repo secret (optional)

```bash
gh secret set FIRECRAWL_API_KEY
# Paste your key when prompted; press Enter
```

If you skip this, the workflow auto-falls-back to local Playwright scraping. It works either way.

### 3. Enable GitHub Pages

```bash
# Via gh CLI:
gh repo edit --enable-pages
# Then in the GitHub web UI: Settings → Pages → Build and deployment → Source: "GitHub Actions"
```

Or via web UI only:
1. Go to your repo → **Settings** → **Pages**
2. Under **Build and deployment**, set **Source** to **GitHub Actions**
3. Save

### 4. Trigger the first deploy

```bash
gh workflow run "Refresh HK TD Rates"
gh run watch                        # watch it complete (~2 min)
```

Or in the web UI:
1. Go to **Actions** tab
2. Click **Refresh HK TD Rates** in the left sidebar
3. Click **Run workflow** → **Run workflow**
4. Wait ~2 minutes

### 5. Visit the site

`https://YOUR_USERNAME.github.io/hk-td-rates/`

The "Refresh now ↗" button in the top-right links straight back to the Actions page so you (or anyone with repo access) can re-trigger a refresh.

---

## What runs and when

- **Daily at 9 am HKT** — `.github/workflows/refresh.yml` fires automatically (`cron: 0 1 * * *`)
- **On-demand** — click "Refresh now ↗" on the page or "Run workflow" in the Actions tab
- **On code push** — any change to `templates/`, `scripts/`, or `providers.yaml` rebuilds and redeploys

Each run:
1. Scrapes 22 providers (Firecrawl primary, Playwright fallback)
2. Merges with StashAway HK aggregator data
3. Commits the fresh `data/latest.json` back to the repo
4. Builds `public/index.html` and deploys to Pages

---

## Cost

**Zero.** Public GitHub repos get unlimited Pages + Actions (subject to fair-use; this workload uses ~2 minutes/day of compute against a 2000 min/month free tier).

If you add a Firecrawl API key, that has its own free tier (typically generous for daily scrapes).

---

## File layout

```
hk-td-rates/
├── .github/workflows/refresh.yml   # cron + manual + on-push deploy
├── README.md                        # this file
├── SKILL.md                         # orchestration playbook
├── providers.yaml                   # 22-provider catalog
├── data/
│   ├── latest.json                  # current snapshot (auto-updated)
│   └── history/                     # append-only audit trail
├── templates/
│   └── editorial.html               # client-facing comparison page
├── scripts/
│   ├── refresh_firecrawl.py         # production refresh (Firecrawl + fallback)
│   ├── refresh_rates.py             # Playwright-only fallback
│   ├── scrape_syfe_live.py          # standalone Syfe scraper
│   ├── verify_pipelines.py          # E2E verification harness
│   └── normalize.py                 # helpers
└── public/                          # built site (overwritten by workflow)
    ├── index.html
    └── latest.json                  # JSON snapshot served alongside the HTML
```

---

## Troubleshooting

**The Actions run failed**
1. Go to **Actions** tab → click the failed run → expand the failed step
2. 99% of failures are either: Firecrawl quota exhausted (check the API dashboard) or a provider site changed structure (the script auto-falls-back to Playwright, but if Playwright also fails, check `data/latest.json` — the prior values are retained and tagged `fetch_status: stale`)

**The site shows old data**
- The HTML is rebuilt each run from `data/latest.json`. If the run succeeded but the page looks stale, your browser is probably caching — hard-reload (Cmd+Shift+R).

**I want to add a provider**
- Edit `providers.yaml`, add a block under `providers:` with the URL + optional `firecrawl_config`. Push to `main`. The next refresh picks it up.

**I want to remove the public URL**
- Either delete the repo or change to private (requires GitHub Pro for private Pages). For "internal-only" without paying, add a Cloudflare Access gate over the Pages URL (free for ≤50 users) — see the handoff doc for the rough steps.

---

## See also

- `HK-TD-Rates-Handoff.md` / `.docx` — full team handoff document with roadmap and ownership
- `SKILL.md` — the orchestration playbook used by Cowork when invoking this skill locally
