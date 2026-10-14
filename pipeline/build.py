#!/usr/bin/env python3
"""After the Bell: merge the technical scan with the news narrative into the site's data.

  build.py <as_of> --news work/<as_of>/news.json     normal run
  build.py <as_of> --no-news                         technicals-only fallback
  build.py <as_of> --news F --check                  validate only, write nothing

Writes docs/data/<as_of>.json and updates docs/data/index.json.
Exit code 2 = the news JSON failed validation (problems on stderr).
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan import fundamentals, stock_row  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
SECTORS = ["Energy", "Information Technology", "Health Care", "Financials", "Industrials", "Materials",
           "Utilities", "Real Estate", "Consumer Staples", "Consumer Discretionary", "Communication Services"]
STANCES = {"Overweight", "Neutral", "Underweight"}
TONES = {"tailwind", "headwind", "mixed"}


def s(v, n):
    """Plain string, trimmed and capped. Never trusted as markup: the page renders it as text."""
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def extract_json(text):
    """Pull the first top-level JSON object out of a model reply (tolerates code fences / prose)."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def validate(news, scored_syms):
    errs, warns = [], []
    out = {"headline": s(news.get("headline"), 120), "summary": s(news.get("summary"), 800)}
    if len(out["headline"]) < 10:
        errs.append("headline missing")
    if len(out["summary"]) < 40:
        errs.append("summary missing")

    fed = news.get("fed") or {}
    out["fed"] = {"rate": s(fed.get("rate"), 30), "next": s(fed.get("next"), 40), "note": s(fed.get("note"), 200)}

    srcs = []
    for x in news.get("sources") or []:
        url = s(x.get("url"), 500)
        if re.match(r"^https?://[^\s]+$", url):
            srcs.append({"title": s(x.get("title"), 200) or url, "url": url})
        else:
            srcs.append(None)  # keep numbering stable; dropped on render
            warns.append(f"bad source url: {url[:60]}")
    out["sources"] = srcs
    ok_ref = lambda refs: [int(r) for r in (refs or []) if isinstance(r, (int, float, str)) and str(r).isdigit()
                           and 1 <= int(r) <= len(srcs) and srcs[int(r) - 1]]

    out["drivers"] = []
    for d in news.get("drivers") or []:
        tone = s(d.get("tone"), 10).lower()
        if tone not in TONES:
            warns.append(f"driver tone {tone!r} -> mixed")
            tone = "mixed"
        if s(d.get("title"), 90) and s(d.get("body"), 700):
            out["drivers"].append({"title": s(d["title"], 90), "body": s(d["body"], 700), "tone": tone,
                                   "sources": ok_ref(d.get("sources"))})
    if len(out["drivers"]) < 3:
        errs.append(f"need >= 3 drivers, got {len(out['drivers'])}")

    sec = news.get("sectors") or {}
    out["sectors"] = {}
    for name in SECTORS:
        x = sec.get(name) or {}
        stance = s(x.get("stance"), 20).title()
        if stance not in STANCES:
            errs.append(f"sector {name}: stance {stance!r}")
            stance = "Neutral"
        out["sectors"][name] = {"stance": stance, "note": s(x.get("note"), 360)}

    out["picks"], seen = [], set()
    for p in news.get("picks") or []:
        sym = s(p.get("sym"), 10).upper().replace(".", "-")
        tier = s(p.get("tier"), 10).lower()
        if sym not in scored_syms:
            warns.append(f"pick {sym} not in universe, dropped")
            continue
        if tier not in ("buy", "wait") or sym in seen:
            warns.append(f"pick {sym}: bad tier {tier!r} or duplicate, dropped")
            continue
        seen.add(sym)
        out["picks"].append({"sym": sym, "tier": tier, "thesis": s(p.get("thesis"), 450)})
    if len(out["picks"]) < 6:
        errs.append(f"need >= 6 valid picks, got {len(out['picks'])}")

    out["watchlist"] = []
    for w in news.get("watchlist") or []:
        sym = s(w.get("sym"), 10).upper().replace(".", "-")
        if sym in scored_syms and sym not in seen:
            seen.add(sym)
            out["watchlist"].append({"sym": sym, "trigger": s(w.get("trigger"), 320)})
        else:
            warns.append(f"watch {sym} dropped")

    out["featured"] = [int(n) for n in (news.get("featuredHeadlines") or [])
                       if isinstance(n, (int, float, str)) and str(n).isdigit()][:12]
    out["avoid"] = [{"name": s(a.get("name"), 60), "detail": s(a.get("detail"), 320)}
                    for a in news.get("avoid") or [] if s(a.get("name"), 60)][:8]
    out["risks"] = [{"title": s(r.get("title"), 90), "body": s(r.get("body"), 500)}
                    for r in news.get("risks") or [] if s(r.get("title"), 90)][:6]
    cal = []
    for c in news.get("calendar") or []:
        d = s(c.get("date"), 10)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", d) and s(c.get("event"), 160):
            cal.append({"date": d, "event": s(c["event"], 160)})
    out["calendar"] = sorted(cal, key=lambda c: c["date"])[:14]
    return out, errs, warns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("as_of")
    ap.add_argument("--news")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    work = ROOT / "work" / a.as_of
    tech = json.loads((work / "technicals.json").read_text())
    scored = pd.read_csv(work / "scored.csv")
    scored_by = {r.yf: r for _, r in scored.iterrows()}
    known = {x["sym"].replace(".", "-"): x for x in tech["candidates"] + tech["watchCandidates"] + tech["megacaps"]}

    def enrich(sym):
        if sym in known:
            return dict(known[sym])
        return stock_row(scored_by[sym], fundamentals(sym))  # outside the pre-fetched pools

    if a.no_news:
        news, errs, warns = None, [], []
    else:
        raw = Path(a.news).read_text()
        try:
            parsed = extract_json(raw)
        except Exception as e:
            print(f"news JSON unreadable: {e}", file=sys.stderr)
            sys.exit(2)
        news, errs, warns = validate(parsed, set(scored_by))
    for w in warns:
        print("warn:", w, file=sys.stderr)
    if errs:
        for e in errs:
            print("error:", e, file=sys.stderr)
        sys.exit(2)
    if a.check:
        print("news OK")
        return

    sectors = []
    for x in tech["sectors"]:
        n = (news or {}).get("sectors", {}).get(x["name"], {})
        sectors.append({**x, "stance": n.get("stance"), "note": n.get("note")})

    # Latest headlines from the feed (already ranked by publisher); shown as text with https links only.
    heads_path = work / "headlines.json"
    feed = json.loads(heads_path.read_text()) if heads_path.exists() else {}
    market = feed.get("market", [])
    chosen = [market[n - 1] for n in (news or {}).get("featured", []) if 1 <= n <= len(market)]
    if len(chosen) < 4:  # fallback: newest reputable items, balanced across topics
        caps = {"Markets": 5, "Fed & rates": 3, "Earnings": 3, "Economy": 2, "Energy": 2, "Sectors": 2, "Commodities": 1}
        used, chosen = {}, []
        for h_ in market:
            t = h_.get("topic")
            if h_.get("reputable") and used.get(t, 0) < caps.get(t, 1):
                used[t] = used.get(t, 0) + 1
                chosen.append(h_)
    headlines = sorted(({"title": s(h_.get("title"), 220), "source": s(h_.get("source"), 60), "url": h_["url"],
                         "time": h_.get("time"), "topic": s(h_.get("topic"), 20)}
                        for h_ in chosen if str(h_.get("url", "")).startswith("https://")),
                       key=lambda x: x["time"] or "", reverse=True)[:14]

    picks = [{**enrich(p["sym"]), "tier": p["tier"], "thesis": p["thesis"]} for p in (news or {}).get("picks", [])]
    picks.sort(key=lambda p: 0 if p["tier"] == "buy" else 1)
    watch = [{**enrich(w["sym"]), "trigger": w["trigger"]} for w in (news or {}).get("watchlist", [])]

    report = {
        "date": a.as_of, "asOf": tech["asOf"], "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe": tech["universe"], "newsAvailable": news is not None,
        "newsAsOf": feed.get("fetchedAt"), "headlines": headlines,
        "headline": (news or {}).get("headline") or "Technical scan only today",
        "summary": (news or {}).get("summary") or
                   "The news step did not finish for this session, so this report carries the technical scan without commentary.",
        "fed": (news or {}).get("fed"), "drivers": (news or {}).get("drivers", []),
        "risks": (news or {}).get("risks", []), "calendar": (news or {}).get("calendar", []),
        "sources": (news or {}).get("sources", []), "avoid": (news or {}).get("avoid", []),
        "tape": tech["tape"], "market": tech["market"], "sectors": sectors, "industries": tech["industries"],
        "themes": tech["themes"], "picks": picks, "watchlist": watch, "megacaps": tech["megacaps"],
        # with no news, fall back to the top technical names so the page still has something to scan
        "screen": [] if news else sorted(tech["candidates"], key=lambda r: -r["score"])[:20],
    }
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / f"{a.as_of}.json").write_text(json.dumps(report, separators=(",", ":")))
    idx_path = DATA / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {"dates": []}
    dates = sorted(set(idx["dates"]) | {a.as_of}, reverse=True)
    idx_path.write_text(json.dumps({"latest": dates[0], "dates": dates,
                                    "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=1))
    print(f"built {a.as_of}: {len(picks)} picks, {len(watch)} watch, news={news is not None}")


if __name__ == "__main__":
    main()
