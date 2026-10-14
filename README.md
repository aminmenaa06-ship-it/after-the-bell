# After the Bell

A daily US stock market report, published after every trading session.

Each weekday after the close, a pipeline scans the S&P 500 and S&P MidCap 400 (~900 stocks) on trend,
momentum, relative strength, ADX, MACD and accumulation, ranks sectors and industries, and pairs the
results with a news narrative researched and written by Claude from cited sources. The site is static
(`docs/`) and renders each edition from `docs/data/<date>.json`; past editions stay available from the
edition picker.

- `pipeline/scan.py`: technical scan (Yahoo Finance data)
- `pipeline/NEWS_PROMPT.md`: instructions for the news step
- `pipeline/build.py`: validates the narrative and merges it with the scan
- `run_daily.sh`: runs the whole thing and publishes

For information and education only. Not investment advice.
