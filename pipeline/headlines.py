#!/usr/bin/env python3
"""After the Bell: timestamped headline feed from Google News RSS (free, no key).

  headlines.py <as_of>

Reads work/<as_of>/technicals.json for the stocks worth checking, then writes
  work/<as_of>/headlines.json   structured feed (also shown on the site)
  work/<as_of>/headlines.md     newest-first digest handed to the news step
"""
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
ET_TZ = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Broad market topics. Kept generic on purpose: the feed decides what today's stories are.
MARKET_QUERIES = [
    ("Markets", "stock market today"), ("Markets", "Wall Street stocks close"), ("Markets", "stock futures"),
    ("Fed & rates", "Federal Reserve interest rates"), ("Fed & rates", "Treasury yields"),
    ("Economy", "inflation jobs economic data"), ("Energy", "oil prices"), ("Commodities", "gold copper prices"),
    ("Earnings", "earnings results shares after hours"), ("Sectors", "sector stocks rally slump"),
]


# Publishers ranked first; everything else is kept only to fill gaps.
REPUTABLE = ["reuters", "bloomberg", "cnbc", "wall street journal", "wsj", "financial times", "associated press",
             "ap news", "barron", "marketwatch", "yahoo finance", "investor's business daily", "investing.com",
             "seeking alpha", "fortune", "business insider", "axios", "new york times", "washington post", "cnn",
             "fox business", "benzinga", "thestreet", "morningstar", "forbes", "the motley fool", "nasdaq",
             "kiplinger", "the economist", "politico", "federal reserve", "zacks", "s&p global", "oilprice",
             "the hill", "abc news", "nbc news", "cbs news", "bbc", "the guardian", "al jazeera"]


def reputable(source):
    s = source.lower()
    return any(r in s for r in REPUTABLE)


def fetch(query, window):
    url = f"https://news.google.com/rss/search?q={quote_plus(query + ' when:' + window)}&hl=en-US&gl=US&ceid=US:en"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    else:
        return []
    out = []
    for it in ET.fromstring(r.content).findall(".//item"):
        title, source = it.findtext("title") or "", it.findtext("source") or ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
        except Exception:
            continue
        link = it.findtext("link") or ""
        if not link.startswith("https://"):
            continue
        out.append({"title": html.unescape(title).strip(), "source": source.strip(), "url": link, "time": ts})
    return out


def key(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:80]


def fmt(ts):
    return ts.astimezone(ET_TZ).strftime("%b %d %I:%M %p ET").replace(" 0", " ")


def main():
    as_of = sys.argv[1]
    work = ROOT / "work" / as_of
    tech = json.loads((work / "technicals.json").read_text())
    now = datetime.now(timezone.utc)

    # Company queries: the strongest candidates, plus mega caps and fallen leaders that moved >3% today.
    pool = sorted(tech["candidates"], key=lambda r: -r["score"])[:22]
    movers = [r for r in tech["megacaps"] + tech["watchCandidates"] if abs(r.get("r1d") or 0) >= 3]
    seen, stocks = set(), []
    for r in pool + movers:
        if r["sym"] not in seen:
            seen.add(r["sym"])
            stocks.append(r)

    def company_query(r):
        name = re.sub(r"\b(Inc\.?|Corp\.?|Corporation|Co\.?|Ltd\.?|plc|Holdings|Group|Company|\(.*?\))", "", r["name"]).strip(" ,.")
        return r["sym"], fetch(f'"{name}" stock', "2d")

    with ThreadPoolExecutor(max_workers=6) as ex:
        market_raw = list(ex.map(lambda q: (q[0], fetch(q[1], "1d")), MARKET_QUERIES))
        company_raw = list(ex.map(company_query, stocks))

    cutoff = now - timedelta(hours=30)
    pool_items, keys = [], set()
    for topic, items in market_raw:
        for it in items:
            k = key(it["title"])
            if it["time"] >= cutoff and k not in keys:
                keys.add(k)
                pool_items.append({**it, "topic": topic, "reputable": reputable(it["source"])})
    # reputable publishers first, at most 10 per topic so one busy topic can't crowd out the rest
    pool_items.sort(key=lambda x: (not x["reputable"], -x["time"].timestamp()))
    per_topic, market = {}, []
    for it in pool_items:
        if per_topic.get(it["topic"], 0) < 10 and (it["reputable"] or len(market) < 30):
            per_topic[it["topic"]] = per_topic.get(it["topic"], 0) + 1
            market.append(it)
    market.sort(key=lambda x: x["time"], reverse=True)

    companies = {}
    for sym, items in company_raw:
        fresh = sorted((it for it in items if it["time"] >= now - timedelta(hours=54) and key(it["title"]) not in keys),
                       key=lambda x: (not reputable(x["source"]), -x["time"].timestamp()))[:4]
        for it in fresh:
            keys.add(key(it["title"]))
        if fresh:
            companies[sym] = sorted(fresh, key=lambda x: x["time"], reverse=True)

    ser = lambda it: {**it, "time": it["time"].isoformat(timespec="seconds")}
    (work / "headlines.json").write_text(json.dumps({
        "fetchedAt": now.isoformat(timespec="seconds"),
        "market": [ser(x) for x in market[:80]],
        "companies": {s: [ser(x) for x in v] for s, v in companies.items()},
    }, indent=1))

    L = [f"# Headline feed, pulled {fmt(now)} (Google News; newest first; titles are unverified leads)", ""]
    L += ["## Market-wide (numbered; use these numbers for featuredHeadlines)"]
    L += [f"- [{i}] {fmt(x['time'])} · {x['source']} · [{x['topic']}] {x['title']}" for i, x in enumerate(market[:60], 1)]
    L += ["", "## Company headlines (strongest candidates and big movers)"]
    for sym, items in companies.items():
        L.append(f"### {sym}")
        L += [f"- {fmt(x['time'])} · {x['source']} · {x['title']}" for x in items]
    (work / "headlines.md").write_text("\n".join(L) + "\n")
    print(f"headlines: {len(market)} market, {sum(len(v) for v in companies.values())} company items across {len(companies)} stocks")


if __name__ == "__main__":
    main()
