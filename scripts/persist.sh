#!/usr/bin/env bash
# Save the robot's memory (SQLite ledger + TikTok token) back to the repo.
# Someone may have pushed code while this run was working: keep THIS run's
# ledger (it knows what was just posted), take everything else from the latest
# main, and retry if another push races us.
set -u
git config user.name  "clip-factory"
git config user.email "actions@users.noreply.github.com"
mkdir -p /tmp/keep
for f in state/factory.db state/tiktok.enc; do [ -e "$f" ] && cp "$f" /tmp/keep/; done
git diff -- config/settings.yml > /tmp/keep/settings.patch || true
for attempt in 1 2 3; do
  git fetch -q origin main
  git reset -q --hard origin/main
  for f in factory.db tiktok.enc; do [ -e /tmp/keep/$f ] && cp /tmp/keep/$f state/$f; done
  [ -s /tmp/keep/settings.patch ] && { git apply --3way /tmp/keep/settings.patch 2>/dev/null || echo "settings patch skipped"; }
  for f in state/factory.db state/tiktok.enc config/settings.yml; do [ -e "$f" ] && git add "$f"; done
  if git diff --staged --quiet; then echo "nothing to save"; exit 0; fi
  git commit -qm "state: $(date -u +%FT%TZ) [skip ci]"
  if git push -q; then echo "state saved"; exit 0; fi
  echo "push raced, retrying ($attempt)"; sleep 5
done
echo "::warning::state could not be saved after 3 tries"
