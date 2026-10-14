#!/bin/zsh
# After the Bell: daily pipeline.
#   1. scan.py      technical scan of ~900 stocks (Yahoo Finance)
#   2. claude -p    news research + narrative as JSON (WebSearch/WebFetch only, no file or shell access)
#   3. build.py     validate + merge into docs/data/<date>.json
#   4. git push     GitHub Pages republishes the site
#
# Usage: run_daily.sh [--dry-run] [--force]
#   --dry-run  run scan + news step, validate, write nothing to docs/, push nothing
#   --force    rebuild and republish an edition that already exists
# Kill switch: create ~/after-the-bell/DISABLE
set -u
ROOT="$HOME/after-the-bell"
cd "$ROOT" || exit 1
export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Optional auth fallback if the keychain is unavailable: CLAUDE_CODE_OAUTH_TOKEN=... (chmod 600)
[[ -f "$HOME/.config/after-the-bell/env" ]] && source "$HOME/.config/after-the-bell/env"

DRY=0; FORCE=0
for a in "$@"; do case $a in --dry-run) DRY=1 ;; --force) FORCE=1 ;; esac; done
mkdir -p logs
LOG="logs/run-$(date +%Y-%m-%d_%H%M).log"
exec > >(tee -a "$LOG") 2>&1
echo "== $(date) start dry=$DRY force=$FORCE"
[[ -e DISABLE ]] && { echo "DISABLE present, exiting"; exit 0; }

[[ $DRY -eq 0 ]] && { git pull --ff-only --quiet || echo "warn: git pull failed, continuing"; }

AS_OF=$(.venv/bin/python pipeline/scan.py | tail -1)
[[ "$AS_OF" =~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' ]] || { echo "scan failed"; exit 1; }
echo "as of $AS_OF"
if [[ -f "docs/data/$AS_OF.json" && $FORCE -eq 0 && $DRY -eq 0 ]]; then
  echo "edition $AS_OF already published (market holiday or rerun); nothing to do"
  exit 0
fi

W="work/$AS_OF"
NEWS="$W/news.json"; [[ $DRY -eq 1 ]] && NEWS="$W/news-dryrun.json"
# Fresh timestamped headline feed (Google News RSS). Non-fatal: the news step still has web search.
.venv/bin/python pipeline/headlines.py "$AS_OF" || { echo "warn: headline feed failed"; rm -f "$W/headlines.md" "$W/headlines.json"; }
.venv/bin/python - "$AS_OF" > "$W/prompt.md" <<'EOF'
import datetime, pathlib, sys
as_of = sys.argv[1]
tpl = pathlib.Path("pipeline/NEWS_PROMPT.md").read_text()
digest = pathlib.Path(f"work/{as_of}/digest.md").read_text()
hp = pathlib.Path(f"work/{as_of}/headlines.md")
heads = hp.read_text() if hp.exists() else "(The headline feed was unavailable this run; rely on WebSearch.)"
now = datetime.datetime.now().astimezone().strftime("%A %B %d, %Y %I:%M %p %Z")
print(tpl.replace("{{AS_OF}}", as_of).replace("{{NOW}}", now).replace("{{DIGEST}}", digest).replace("{{HEADLINES}}", heads))
EOF

NEWS_OK=0
for attempt in 1 2; do
  echo "news step, attempt $attempt ($(date +%T))"
  # 25-minute hard cap; the model gets web tools only.
  perl -e 'alarm shift; exec @ARGV' 1500 \
    claude -p --tools "WebSearch,WebFetch" --allowedTools "WebSearch,WebFetch" --output-format json \
    < "$W/prompt.md" > "$W/claude-$attempt.json"
  .venv/bin/python -c 'import json,sys; d=json.load(open(sys.argv[1])); print("cost_usd", d.get("total_cost_usd"), "turns", d.get("num_turns"), file=sys.stderr); open(sys.argv[2],"w").write(d.get("result") or "")' \
    "$W/claude-$attempt.json" "$NEWS" || { echo "claude returned no usable output"; continue; }
  if .venv/bin/python pipeline/build.py "$AS_OF" --news "$NEWS" --check; then NEWS_OK=1; break; fi
done
echo "news ok: $NEWS_OK"

if [[ $DRY -eq 1 ]]; then echo "dry run complete, nothing published"; exit 0; fi

if [[ $NEWS_OK -eq 1 ]]; then
  .venv/bin/python pipeline/build.py "$AS_OF" --news "$NEWS" || exit 1
else
  .venv/bin/python pipeline/build.py "$AS_OF" --no-news || exit 1
fi

git add docs/data
if git commit -q -m "Edition for $AS_OF"; then
  git push -q origin main && echo "published $AS_OF" || { echo "push failed"; exit 1; }
else
  echo "nothing to commit"
fi
echo "== $(date) done"
