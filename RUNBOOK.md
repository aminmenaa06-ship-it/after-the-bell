# Operating After the Bell

Site: https://aminmenaa06-ship-it.github.io/after-the-bell/
Repo: https://github.com/aminmenaa06-ship-it/after-the-bell (public; GitHub Pages serves `docs/` on `main`)

## Schedule
LaunchAgent `com.amin.afterthebell` runs `run_daily.sh` Monday–Friday at 2:30 PM Pacific (5:30 PM ET).
If the Mac is asleep at that time, launchd runs it on wake. If it is powered off, that day is skipped;
run it by hand later (the scan always uses the most recent completed session).
Market holidays need no handling: the scan's as-of date doesn't change, so the run exits without publishing.

## Commands
- Run now: `~/after-the-bell/run_daily.sh`
- Test without publishing: `~/after-the-bell/run_daily.sh --dry-run`
- Rebuild today's edition: `~/after-the-bell/run_daily.sh --force`
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

## Troubleshooting
- `claude` exits with "Not logged in": launchd normally reads the login keychain. Fallback: run
  `claude setup-token`, then put `CLAUDE_CODE_OAUTH_TOKEN=...` in `~/.config/after-the-bell/env`
  (chmod 600); `run_daily.sh` sources it.
- Push fails: check `gh auth status` and that `git push` works from a terminal in this folder.
- Yahoo rate limits: rerun later; `scan.py` retries each download chunk three times.
