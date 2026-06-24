#!/usr/bin/env bash
# One-shot setup for HK TD Rates GitHub Pages deploy.
# Run this once from the unzipped hk-td-rates folder. Idempotent.

set -euo pipefail

REPO_NAME="${REPO_NAME:-hk-td-rates}"
VISIBILITY="${VISIBILITY:-public}"   # public is required for free Pages

say() { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }
ok()  { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn(){ printf "  \033[33m!\033[0m %s\n" "$*"; }

# -- 1. Prerequisites ----------------------------------------------------------
say "Checking prerequisites"
command -v git >/dev/null || { echo "git not installed — install from https://git-scm.com/"; exit 1; }
ok "git found"
if ! command -v gh >/dev/null; then
  warn "gh CLI not found. Install with: brew install gh   (then re-run this script)"
  echo "  Or follow the README's manual web-UI instructions instead."
  exit 1
fi
ok "gh CLI found"

# -- 2. Authenticate -----------------------------------------------------------
say "GitHub authentication"
if ! gh auth status >/dev/null 2>&1; then
  warn "Not authenticated — launching browser login (one time)"
  gh auth login --web --git-protocol https
fi
GH_USER=$(gh api user -q .login)
ok "Signed in as $GH_USER"

# -- 3. Init repo + first commit ----------------------------------------------
say "Preparing git repo"
if [ ! -d .git ]; then
  git init -q
  git branch -M main
  ok "Initialized git"
else
  ok "Git already initialized"
fi

if [ -z "$(git status --porcelain)" ] && git rev-parse HEAD >/dev/null 2>&1; then
  ok "Working tree clean"
else
  git add -A
  git -c user.email="$GH_USER@users.noreply.github.com" \
      -c user.name="$GH_USER" \
      commit -q -m "Initial commit: HK TD rates" || ok "Nothing to commit"
fi

# -- 4. Create the GitHub repo (or skip if exists) -----------------------------
say "Creating repository $GH_USER/$REPO_NAME"
if gh repo view "$GH_USER/$REPO_NAME" >/dev/null 2>&1; then
  warn "Repo already exists — skipping create"
  if ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin "https://github.com/$GH_USER/$REPO_NAME.git"
  fi
else
  gh repo create "$REPO_NAME" --"$VISIBILITY" --source=. --remote=origin --push --description "HK Time Deposit Rates · Syfe vs. competitors"
  ok "Repo created and pushed"
fi
# If repo existed, push current commits
git push -u origin main || true

# -- 5. Optional: Firecrawl secret ---------------------------------------------
say "Firecrawl API key (optional)"
if gh secret list -R "$GH_USER/$REPO_NAME" | grep -q FIRECRAWL_API_KEY; then
  ok "FIRECRAWL_API_KEY already set"
else
  echo "  If you have a Firecrawl key, paste it now. Otherwise press Enter to skip."
  read -r -s -p "  FIRECRAWL_API_KEY (input hidden, Enter to skip): " FC_KEY
  echo
  if [ -n "${FC_KEY:-}" ]; then
    printf "%s" "$FC_KEY" | gh secret set FIRECRAWL_API_KEY -R "$GH_USER/$REPO_NAME"
    ok "Secret saved"
  else
    warn "Skipped — workflow will use Playwright fallback"
  fi
fi

# -- 6. Enable Pages via API ---------------------------------------------------
say "Enabling GitHub Pages (source: GitHub Actions)"
if gh api -X POST "repos/$GH_USER/$REPO_NAME/pages" \
   -f "build_type=workflow" >/dev/null 2>&1; then
  ok "Pages enabled"
elif gh api -X PUT "repos/$GH_USER/$REPO_NAME/pages" \
   -f "build_type=workflow" >/dev/null 2>&1; then
  ok "Pages updated to use Actions"
else
  warn "Could not enable Pages via API."
  warn "Open: https://github.com/$GH_USER/$REPO_NAME/settings/pages"
  warn "Set Source → GitHub Actions, then come back."
  read -r -p "  Press Enter once done..." _
fi

# -- 7. Trigger the first run --------------------------------------------------
say "Triggering first workflow run"
gh workflow run "Refresh HK TD Rates" -R "$GH_USER/$REPO_NAME" || \
  gh workflow run refresh.yml -R "$GH_USER/$REPO_NAME"
ok "Dispatched — watching for ~2 min"
sleep 5
gh run watch -R "$GH_USER/$REPO_NAME" --exit-status || warn "Run errored — see Actions tab"

# -- 8. Print the URL ----------------------------------------------------------
SITE_URL="https://$GH_USER.github.io/$REPO_NAME/"
ACTIONS_URL="https://github.com/$GH_USER/$REPO_NAME/actions/workflows/refresh.yml"

cat <<EOF

═══════════════════════════════════════════════════════════════════
✅ Live at:           $SITE_URL
🔄 Refresh on-demand: $ACTIONS_URL
📅 Auto-refresh:      9am HKT daily
═══════════════════════════════════════════════════════════════════

EOF

# Try to open the site
if command -v open >/dev/null; then open "$SITE_URL"; fi
