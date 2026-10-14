# Operating After the Bell

Site: https://aminmenaa06-ship-it.github.io/after-the-bell/
Repo: https://github.com/aminmenaa06-ship-it/after-the-bell (public; GitHub Pages serves `docs/` on `main`)

## Schedule
LaunchAgent `com.amin.afterthebell` runs `run_daily.sh` Monday–Friday at 4:00 AM Pacific (7:00 AM ET).
The pre-market edition is live around 7:20 AM ET (worst case ~7:55 if the news step retries), about two
hours before the 9:30 open. It uses technicals from the previous close plus news through ~7:10 AM ET.
Weekends and NYSE holidays are skipped (`pipeline/session.py`, exchange_calendars).

Keep the Mac awake at 4 AM: set a repeating wake once (needs your password):
`sudo pmset repeat wakeorpoweron MTWRF 03:55:00`  (check with `pmset -g sched`; remove with `sudo pmset repeat cancel`).
Without it, a sleeping Mac runs the job when it next wakes, possibly after the open; the page always
shows when each edition was built. `run_daily.sh` holds a caffeinate assertion for the length of the run.

Editions: `docs/data/<date>-pre.json` (pre-market) and `docs/data/<date>.json` (closing, only from a manual
run after 4 PM ET, e.g. `run_daily.sh` in the evening). `docs/data/index.json` lists both for the archive picker.

## Commands
- Run now: `~/after-the-bell/run_daily.sh`
- Test without publishing: `~/after-the-bell/run_daily.sh --dry-run`
- Rebuild today's edition: `~/after-the-bell/run_daily.sh` (pre-market editions are refreshed on every run)
- Force a closing edition: `~/after-the-bell/run_daily.sh --mode close --force`
- Pause: `touch ~/after-the-bell/DISABLE` (resume: delete the file)
- Status: `launchctl print gui/$(id -u)/com.amin.afterthebell | grep -E "state|last exit"`
- Unload: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.amin.afterthebell.plist`
- Load: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.amin.afterthebell.plist`
- Logs: `~/after-the-bell/logs/run-*.log` (one per run), plus `logs/launchd.*.log`

## How a run works
1. `pipeline/scan.py` downloads 2 years of daily bars for the S&P 500 + MidCap 400 (constituents from
   Wikipedia, cached in `work/universe.csv`), sector/theme ETFs and macro tickers, scores every stock and
   writes `work/<date>/{scored.csv, technicals.json, digest.md}`.
2. `pipeline/headlines.py` pulls a timestamped headline feed from Google News RSS (free, no key): broad
   market topics plus the ~25 strongest candidates, ranked by a reputable-publisher list
   (`work/<date>/headlines.{json,md}`). If it fails, the run continues on web search alone.
   (The `newsmcp` MCP server was evaluated on 2026-10-14: its API returns HTTP 410, service shut down.
   Alpha Vantage's news endpoint is premium-only on this account.)
3. `claude -p` gets `pipeline/NEWS_PROMPT.md` + the digest + the headline feed on stdin, with only
   WebSearch and WebFetch available (no shell, no file writes). It replies with one JSON object
   (the narrative, plus `featuredHeadlines` chosen from the feed for the site's Latest headlines list).
4. `pipeline/build.py --check` validates it (11 sectors, ≥6 picks that exist in the scan, http(s)
   sources...). Two attempts; if both fail the edition publishes as technicals-only with a notice.
5. `build.py` merges and writes `docs/data/<date>.json` + `docs/data/index.json`; the script commits
   and pushes; Pages republishes within a minute or two.

Every number attached to a stock comes from the scan; the narrative only chooses symbols and writes text.
The page renders all data with textContent, never as HTML.

## When the news step fails
The news step shares your Claude plan's usage limits. If a run hits a limit (or the reply can't be
validated twice), the script publishes the numbers-only edition right away so the page is never empty,
then retries the news step every 15 minutes until 9:15 AM ET and republishes the full edition as soon
as one attempt succeeds. Heavy Claude use late at night can push the morning write-up past its
7:20 AM ET target. To use less of the plan per run, set a lighter model in
`~/.config/after-the-bell/env`: `ATB_MODEL=claude-sonnet-5-5`.

## Troubleshooting
- `claude` exits with "Not logged in": launchd normally reads the login keychain. Fallback: run
  `claude setup-token`, then put `CLAUDE_CODE_OAUTH_TOKEN=...` in `~/.config/after-the-bell/env`
  (chmod 600); `run_daily.sh` sources it.
- Push fails: check `gh auth status` and that `git push` works from a terminal in this folder.
- Yahoo rate limits: rerun later; `scan.py` retries each download chunk three times.
