#!/bin/zsh
# After the Bell: daily pipeline. Scheduled for 4:00 AM Pacific (7:00 AM ET) on weekdays so the
# pre-market edition is live around 7:20 AM ET, about two hours before the 9:30 open.
#   1. session.py    which edition: pre-market (before the close), closing (after), or none (holiday)
#   2. scan.py       technical scan of ~900 stocks as of the last completed session (Yahoo Finance)
#   3. headlines.py  timestamped headline feed since the last close (Google News RSS)
#   4. claude -p     news research + narrative as JSON (WebSearch/WebFetch only, no file or shell access)
#   5. build.py      validate + merge into docs/data/<edition>.json
#   6. git push      GitHub Pages republishes the site
#
# Usage: run_daily.sh [--dry-run] [--force] [--mode premarket|close]
#   --dry-run  run everything except building into docs/ and publishing
#   --force    publish even on a closed day, or rebuild a closing edition that already exists
#   --mode     override the edition type session.py picks
# Kill switch: create ~/after-the-bell/DISABLE
set -u
ROOT="$HOME/after-the-bell"
cd "$ROOT" || exit 1
export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Optional auth fallback if the keychain is unavailable: CLAUDE_CODE_OAUTH_TOKEN=... (chmod 600)
[[ -f "$HOME/.config/after-the-bell/env" ]] && source "$HOME/.config/after-the-bell/env"
# Keep the Mac awake for the whole run (it may have just woken from a scheduled wake).
caffeinate -i -s -w $$ &

DRY=0; FORCE=0; MODE_OVERRIDE=""
while (( $# )); do
  case $1 in
    --dry-run) DRY=1 ;; --force) FORCE=1 ;;
    --mode) shift; MODE_OVERRIDE="${1:-}" ;;
  esac
  shift
done
mkdir -p logs
LOG="logs/run-$(date +%Y-%m-%d_%H%M).log"
exec > >(tee -a "$LOG") 2>&1
echo "== $(date) start dry=$DRY force=$FORCE"
[[ -e DISABLE ]] && { echo "DISABLE present, exiting"; exit 0; }

read MODE SESSION <<< "$(.venv/bin/python pipeline/session.py)"
if [[ "$MODE" == "closed" ]]; then
  [[ $FORCE -eq 1 || -n "$MODE_OVERRIDE" ]] || { echo "market closed on $SESSION (weekend or holiday); nothing to do"; exit 0; }
  MODE="premarket"
fi
[[ -n "$MODE_OVERRIDE" ]] && MODE="$MODE_OVERRIDE"
echo "edition: $MODE for session $SESSION"

[[ $DRY -eq 0 ]] && { git pull --ff-only --quiet || echo "warn: git pull failed, continuing"; }

AS_OF=$(.venv/bin/python pipeline/scan.py | tail -1)
[[ "$AS_OF" =~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' ]] || { echo "scan failed"; exit 1; }
echo "prices as of $AS_OF close"
if [[ "$MODE" == "close" ]]; then
  SESSION="$AS_OF"; ID="$AS_OF"
  if [[ -f "docs/data/$ID.json" && $FORCE -eq 0 && $DRY -eq 0 ]]; then echo "closing edition $ID already published"; exit 0; fi
else
  ID="$SESSION-pre"   # one pre-market edition per day; a rerun refreshes it
fi

W="work/$AS_OF"
NEWS="$W/news-$MODE.json"; [[ $DRY -eq 1 ]] && NEWS="$W/news-$MODE-dryrun.json"
.venv/bin/python pipeline/headlines.py "$AS_OF" || { echo "warn: headline feed failed"; rm -f "$W/headlines.md" "$W/headlines.json"; }
.venv/bin/python - "$AS_OF" "$SESSION" "$MODE" > "$W/prompt-$MODE.md" <<'EOF'
import datetime, pathlib, sys
as_of, session, mode = sys.argv[1:4]
fmt = lambda d: datetime.date.fromisoformat(d).strftime("%A, %B %d, %Y")
contexts = {
    "premarket": (f"This is the PRE-MARKET edition for the {fmt(session)} trading session. Readers open it between "
                  f"about 7:30 and 9:30 AM ET to plan the day before the 9:30 AM ET open. Prices and technicals are from "
                  f"the {fmt(as_of)} close. Your research must cover everything since that close: US stock futures, the "
                  f"Asian and European sessions, pre-market movers, earnings released after yesterday's close and before "
                  f"today's open, analyst rating changes, and the economic data and Fed speakers scheduled for today "
                  f"(with their times). Write the headline and summary about what matters for today's session."),
    "close": (f"This is the CLOSING edition for the {fmt(as_of)} session, published after the 4 PM ET close. Cover what "
              f"happened in the session and after the bell (after-hours earnings, late news), and set up the next session."),
}
tpl = pathlib.Path("pipeline/NEWS_PROMPT.md").read_text()
digest = pathlib.Path(f"work/{as_of}/digest.md").read_text()
hp = pathlib.Path(f"work/{as_of}/headlines.md")
heads = hp.read_text() if hp.exists() else "(The headline feed was unavailable this run; rely on WebSearch.)"
now = datetime.datetime.now().astimezone().strftime("%A %B %d, %Y %I:%M %p %Z")
out = (tpl.replace("{{EDITION_CONTEXT}}", contexts[mode]).replace("{{AS_OF}}", as_of).replace("{{NOW}}", now)
          .replace("{{DIGEST}}", digest).replace("{{HEADLINES}}", heads))
print(out)
EOF

NEWS_OK=0
for attempt in 1 2; do
  echo "news step, attempt $attempt ($(date +%T))"
  # 25-minute hard cap; the model gets web tools only.
  perl -e 'alarm shift; exec @ARGV' 1500 \
    claude -p --tools "WebSearch,WebFetch" --allowedTools "WebSearch,WebFetch" --output-format json \
    < "$W/prompt-$MODE.md" > "$W/claude-$MODE-$attempt.json"
  .venv/bin/python -c 'import json,sys; d=json.load(open(sys.argv[1])); print("cost_usd", d.get("total_cost_usd"), "turns", d.get("num_turns"), file=sys.stderr); open(sys.argv[2],"w").write(d.get("result") or "")' \
    "$W/claude-$MODE-$attempt.json" "$NEWS" || { echo "claude returned no usable output"; continue; }
  if .venv/bin/python pipeline/build.py "$AS_OF" --edition "$MODE" --session "$SESSION" --news "$NEWS" --check; then NEWS_OK=1; break; fi
done
echo "news ok: $NEWS_OK"

if [[ $DRY -eq 1 ]]; then echo "dry run complete, nothing published"; exit 0; fi

if [[ $NEWS_OK -eq 1 ]]; then
  .venv/bin/python pipeline/build.py "$AS_OF" --edition "$MODE" --session "$SESSION" --news "$NEWS" || exit 1
else
  .venv/bin/python pipeline/build.py "$AS_OF" --edition "$MODE" --session "$SESSION" --no-news || exit 1
fi

git add docs/data
if git commit -q -m "$MODE edition $ID"; then
  git push -q origin main && echo "published $ID" || { echo "push failed"; exit 1; }
else
  echo "nothing to commit"
fi
echo "== $(date) done"
