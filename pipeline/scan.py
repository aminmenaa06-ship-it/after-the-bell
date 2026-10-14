#!/usr/bin/env python3
"""After the Bell: daily technical scan.

Pulls ~2 years of daily bars for the S&P 500 + S&P MidCap 400, sector/theme ETFs and
macro tickers from Yahoo Finance, scores every stock, and writes:

  work/<as_of>/scored.csv        every stock with every indicator
  work/<as_of>/technicals.json   everything the site needs except the news narrative
  work/<as_of>/digest.md         compact summary handed to Claude for the news step

Prints the as-of date (last completed session) on the final line.
"""
import io
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

SECTOR_ETF = {
    "Energy": "XLE", "Information Technology": "XLK", "Health Care": "XLV", "Financials": "XLF",
    "Industrials": "XLI", "Materials": "XLB", "Utilities": "XLU", "Real Estate": "XLRE",
    "Consumer Staples": "XLP", "Consumer Discretionary": "XLY", "Communication Services": "XLC",
}
THEME_ETF = {
    "SMH": "Semiconductors", "IGV": "Software", "CIBR": "Cybersecurity", "XBI": "Biotech",
    "IHI": "Medical devices", "ITA": "Aerospace & defense", "XOP": "Oil & gas producers",
    "OIH": "Oil services", "KRE": "Regional banks", "KBE": "Banks", "ITB": "Home construction",
    "XHB": "Homebuilders & suppliers", "GDX": "Gold miners", "COPX": "Copper miners",
    "XME": "Metals & mining", "XRT": "Retail", "IYT": "Transports", "JETS": "Airlines",
    "TAN": "Solar", "URA": "Uranium", "TLT": "Long Treasuries", "GLD": "Gold",
    "IWM": "Small caps", "RSP": "S&P 500 equal weight", "QQQ": "Nasdaq 100",
}
TAPE = [  # (yahoo symbol, label, unit). Futures first: they trade overnight, so they are live pre-market.
    ("ES=F", "S&P 500 futures", ""), ("NQ=F", "Nasdaq 100 futures", ""),
    ("^GSPC", "S&P 500", ""), ("^IXIC", "Nasdaq", ""), ("^DJI", "Dow", ""), ("^RUT", "Russell 2000", ""),
    ("^VIX", "VIX", ""), ("^TNX", "10-yr yield", "%"), ("DX-Y.NYB", "Dollar index", ""),
    ("BZ=F", "Brent", "$"), ("CL=F", "WTI crude", "$"), ("GC=F", "Gold", "$"), ("HG=F", "Copper", "$"),
    ("BTC-USD", "Bitcoin", "$"),
]
MEGA = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "BRK-B", "JPM", "LLY", "V",
        "XOM", "WMT", "MA", "ORCL", "NFLX", "COST", "UNH", "AMD", "MU", "PLTR", "GS", "CAT", "GE",
        "RTX", "LMT", "HD", "MCD", "BA", "CVX", "INTC", "CSCO", "CRM", "ADBE", "IBM"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- universe + data
def load_universe():
    cache = WORK / "universe.csv"

    def wiki(url, idx):
        html = requests.get(url, headers=UA, timeout=30).text
        for t in pd.read_html(io.StringIO(html)):
            cols = [str(c) for c in t.columns]
            if "Symbol" in cols and "GICS Sector" in cols:
                name_col = "Security" if "Security" in cols else ("Company" if "Company" in cols else cols[1])
                df = t[["Symbol", name_col, "GICS Sector", "GICS Sub-Industry"]].copy()
                df.columns = ["symbol", "name", "sector", "industry"]
                df["index"] = idx
                return df
        raise RuntimeError(f"no constituent table at {url}")

    try:
        uni = pd.concat([
            wiki("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "SP500"),
            wiki("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "SP400"),
        ]).drop_duplicates("symbol")
        uni["yf"] = uni["symbol"].str.replace(".", "-", regex=False)
        uni.to_csv(cache, index=False)
    except Exception as e:  # Wikipedia down: fall back to the last good list
        log("universe fetch failed, using cache:", e)
        uni = pd.read_csv(cache)
    return uni


def download(tickers):
    frames = []
    for i in range(0, len(tickers), 150):
        chunk = tickers[i:i + 150]
        for attempt in range(3):
            try:
                frames.append(yf.download(chunk, period="2y", interval="1d", auto_adjust=True,
                                          group_by="ticker", threads=True, progress=False))
                break
            except Exception as e:
                log("download retry", i, e)
                time.sleep(5 * (attempt + 1))
    return pd.concat(frames, axis=1)


# ---------------------------------------------------------------- indicators
def wilder(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def indicators(df, spy):
    df = df.dropna(subset=["Close"])
    if len(df) < 260:
        return None
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"].fillna(0)
    sma20, sma50, sma150, sma200 = (c.rolling(n).mean() for n in (20, 50, 150, 200))
    d = c.diff()
    rsi = 100 - 100 / (1 + wilder(d.clip(lower=0), 14) / wilder(-d.clip(upper=0), 14))
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = wilder(tr, 14)
    up, dn = h.diff(), -l.diff()
    pdi = 100 * wilder(up.where((up > dn) & (up > 0), 0.0), 14) / atr
    mdi = 100 * wilder(dn.where((dn > up) & (dn > 0), 0.0), 14) / atr
    adx = wilder(100 * (pdi - mdi).abs() / (pdi + mdi), 14)
    bbw_pct = ((4 * c.rolling(20).std()) / sma20).rolling(126).rank(pct=True)
    chg = c.diff()
    ud = v.where(chg > 0, 0).rolling(50).sum() / v.where(chg < 0, 0).rolling(50).sum().replace(0, np.nan)
    obv = (np.sign(chg).fillna(0) * v).cumsum()
    obv_slope = (obv.iloc[-1] - obv.iloc[-21]) / (v.rolling(50).mean().iloc[-1] * 20 + 1e-9)
    rsline = c / spy.reindex(c.index).ffill()
    hi52, lo52 = c.iloc[-252:].max(), c.iloc[-252:].min()
    last = lambda x: float(x.iloc[-1])
    roc = lambda n: last(c) / float(c.iloc[-1 - n]) - 1
    gc = (sma50 > sma200).astype(int).diff()
    a200 = (c > sma200).astype(int).diff()
    mx = (hist > 0).astype(int).diff()
    return dict(
        close=last(c), sma50=last(sma50), sma200=last(sma200),
        pct_vs_50=last(c) / last(sma50) - 1, pct_vs_200=last(c) / last(sma200) - 1,
        sma200_rising=last(sma200) > float(sma200.iloc[-22]),
        tt_c_gt_150_200=last(c) > last(sma150) and last(c) > last(sma200),
        tt_150_gt_200=last(sma150) > last(sma200),
        tt_50_gt_150=last(sma50) > last(sma150) and last(sma50) > last(sma200),
        tt_c_gt_50=last(c) > last(sma50),
        tt_30_above_low=last(c) >= lo52 * 1.30,
        tt_within_25_high=last(c) >= hi52 * 0.75,
        off_high=last(c) / hi52 - 1, hi52=float(hi52),
        rsi=last(rsi), macd=last(macd), macd_hist=last(hist), macd_hist_rising=last(hist) > float(hist.iloc[-4]),
        adx=last(adx), pdi=last(pdi), mdi=last(mdi), atr_pct=last(atr) / last(c),
        bbw_pct=last(bbw_pct), ud_ratio=last(ud), obv_slope=float(obv_slope),
        r1d=roc(1), r1w=roc(5), r1m=roc(21), r3m=roc(63), r6m=roc(126), r9m=roc(189), r12m=roc(251),
        rs_line_nh=bool(rsline.iloc[-1] >= rsline.iloc[-252:].max() * 0.98),
        golden_cross_30d=bool((gc.iloc[-30:] == 1).any()),
        reclaim200_30d=bool((a200.iloc[-30:] == 1).any()) and last(c) > last(sma200),
        macd_bull_x_10d=bool((mx.iloc[-10:] == 1).any()),
        dollar_vol=float((c * v).iloc[-50:].mean()),
    )


def score(df):
    df["rs_raw"] = 0.4 * df.r3m + 0.2 * df.r6m + 0.2 * df.r9m + 0.2 * df.r12m
    df["rs_rating"] = (df.rs_raw.rank(pct=True) * 98 + 1).round().astype(int)
    tt_cols = [c for c in df.columns if c.startswith("tt_")] + ["sma200_rising"]
    df["tt_score"] = df[tt_cols].sum(axis=1) + (df.rs_rating >= 70).astype(int)
    trend = df.tt_score / 8 * 30
    mom = df.rs_rating / 99 * 25
    macd_pts = np.where(df.macd_hist > 0, 5, 0) + np.where(df.macd_hist_rising, 2, 0) + np.where(df.macd > 0, 3, 0)
    adx_pts = np.where((df.pdi > df.mdi) & (df.adx >= 20), np.clip(df.adx, 20, 40) / 4, np.where(df.pdi > df.mdi, 3, 0))
    accum = np.clip((df.ud_ratio - 0.8) / 0.6, 0, 1) * 6 + np.clip(df.obv_slope, 0, 1) * 4
    rsi_pts = np.where(df.rsi.between(50, 70), 5, np.where(df.rsi.between(70, 78) | df.rsi.between(45, 50), 2.5, 0))
    near_high = np.where(df.off_high >= -0.10, 3, np.where(df.off_high >= -0.20, 1.5, 0))
    squeeze = np.where(df.bbw_pct <= 0.25, 2, 0)
    ext_pen = np.clip((df.pct_vs_50 - 0.15) * 40, 0, 10) + np.where(df.rsi > 80, 4, 0)
    df["score"] = (trend + mom + macd_pts + adx_pts + accum + rsi_pts + near_high + squeeze - ext_pen).round(1)
    # likely pinned by a pending deal: near-zero daily range and no drift
    df["pinned"] = (df.atr_pct < 0.006) & (df.r1m.abs() < 0.03)

    def setup(r):
        if r.tt_score >= 7 and r.rsi <= 74 and r.pct_vs_50 <= 0.15 and r.macd_hist > -0.5 * abs(r.macd):
            return "Leader"
        if r.tt_score >= 7:
            return "Extended"
        if (r.golden_cross_30d or r.reclaim200_30d or r.macd_bull_x_10d) and r.close > r.sma200 and r.r3m > r.r6m / 2:
            return "Emerging"
        if r.close < r.sma200 and not r.sma200_rising:
            return "Downtrend"
        return "Neutral"

    df["setup"] = df.apply(setup, axis=1)
    return df


# ---------------------------------------------------------------- analyst + earnings
def fundamentals(sym):
    out = {"sym": sym}
    try:
        t = yf.Ticker(sym)
        info = t.info or {}
        out.update(target=info.get("targetMeanPrice"), rating=info.get("recommendationKey"),
                   rating_mean=info.get("recommendationMean"), n_analysts=info.get("numberOfAnalystOpinions"),
                   long_name=info.get("shortName") or info.get("longName"))
        try:
            cal = t.calendar or {}
            ed = cal.get("Earnings Date") or []
            out["earnings"] = str(ed[0]) if ed else None
        except Exception:
            out["earnings"] = None
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


# ---------------------------------------------------------------- helpers
def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else round(f, 6)
    return o


def pct(x, nd=1):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x) * 100, nd)


def stock_row(r, fund=None):
    f = fund or {}
    atr = r.atr_pct * r.close
    risk = max(r.sma50, r.close - 2.5 * atr) if r.close > r.sma50 else r.close - 2.5 * atr
    target = f.get("target")
    return clean(dict(
        sym=r.symbol, name=r["name"], sector=r.sector, industry=r.industry, index=r["index"],
        close=round(r.close, 2), score=r.score, setup=r.setup, tt=int(r.tt_score), rs=int(r.rs_rating),
        rsi=round(r.rsi), adx=round(r.adx), vs50=pct(r.pct_vs_50), vs200=pct(r.pct_vs_200), offHigh=pct(r.off_high),
        r1d=pct(r.r1d, 2), r1m=pct(r.r1m), r3m=pct(r.r3m), r12m=pct(r.r12m), atrPct=pct(r.atr_pct, 2),
        risk=round(risk, 2), riskPct=pct(risk / r.close - 1), pinned=bool(r.pinned),
        earnings=(f.get("earnings") or None),
        analysts=None if not f.get("n_analysts") else dict(
            n=f.get("n_analysts"), rating=f.get("rating"), mean=f.get("rating_mean"),
            target=target, upside=pct(target / r.close - 1) if target else None),
    ))


# ---------------------------------------------------------------- main
def main():
    uni = load_universe()
    log("universe", len(uni))
    etfs = list(SECTOR_ETF.values()) + list(THEME_ETF) + ["SPY"]
    tape_syms = [t[0] for t in TAPE]
    data = download(list(uni.yf) + etfs + tape_syms)
    spy = data["SPY"]["Close"].dropna()
    as_of = spy.index[-1].strftime("%Y-%m-%d")
    out_dir = WORK / as_of
    out_dir.mkdir(parents=True, exist_ok=True)
    log("as of", as_of)

    rows = []
    for _, u in uni.iterrows():
        try:
            r = indicators(data[u.yf], spy)
        except KeyError:
            r = None
        if r:
            rows.append({**u.to_dict(), **r})
    df = score(pd.DataFrame(rows))
    df.to_csv(out_dir / "scored.csv", index=False)
    log("scored", len(df))

    # ---- breadth history (last 130 sessions)
    closes = pd.DataFrame({s: data[s]["Close"] for s in df.yf if s in data.columns.get_level_values(0)})
    # align to trading days: the frame also carries weekend rows from 24/7 tickers (BTC-USD)
    closes = closes.reindex(spy.index).ffill(limit=3)
    a50 = (closes > closes.rolling(50).mean()).where(closes.notna())
    a200 = (closes > closes.rolling(200).mean()).where(closes.notna())
    idx = spy.index[-130:]
    series = dict(
        dates=[d.strftime("%Y-%m-%d") for d in idx],
        spx=[round(float(x), 2) for x in data["^GSPC"]["Close"].reindex(idx).ffill()],
        above50=[round(float(x) * 100, 1) for x in a50.mean(axis=1).reindex(idx)],
        above200=[round(float(x) * 100, 1) for x in a200.mean(axis=1).reindex(idx)],
    )

    # ---- tape
    tape = []
    for sym, label, unit in TAPE:
        try:
            s = data[sym]["Close"].dropna()
            tape.append(dict(sym=sym, label=label, unit=unit, value=round(float(s.iloc[-1]), 2),
                             d1=pct(s.iloc[-1] / s.iloc[-2] - 1, 2), r1m=pct(s.iloc[-1] / s.iloc[-22] - 1),
                             spark=[round(float(x), 2) for x in s.iloc[-30:]]))
        except Exception as e:
            log("tape miss", sym, e)

    # ---- index snapshot
    gspc = data["^GSPC"]["Close"].dropna()
    year_start = gspc[gspc.index.year == gspc.index[-1].year].iloc[0]
    spx = dict(close=round(float(gspc.iloc[-1]), 2), d1=pct(gspc.iloc[-1] / gspc.iloc[-2] - 1, 2),
               offHigh=pct(gspc.iloc[-1] / gspc.iloc[-252:].max() - 1), ytd=pct(gspc.iloc[-1] / year_start - 1),
               r1m=pct(gspc.iloc[-1] / gspc.iloc[-22] - 1), r3m=pct(gspc.iloc[-1] / gspc.iloc[-64] - 1))

    def breadth(g):
        return dict(n=int(len(g)), above50=pct((g.close > g.sma50).mean(), 0), above200=pct((g.close > g.sma200).mean(), 0),
                    nearHigh=pct((g.off_high > -0.05).mean(), 0), downtrend=pct((g.setup == "Downtrend").mean(), 0))

    market = dict(spx=spx, breadth=dict(all=breadth(df), sp500=breadth(df[df["index"] == "SP500"]),
                                        sp400=breadth(df[df["index"] == "SP400"])), series=series)

    # ---- ETF stats
    def etf_stats(sym):
        r = indicators(data[sym], spy)
        if not r:
            return None
        trend = "Up" if r["close"] > r["sma200"] and r["sma200_rising"] and r["close"] > r["sma50"] else (
            "Down" if r["close"] < r["sma200"] and not r["sma200_rising"] else "Mixed")
        return clean(dict(sym=sym, close=round(r["close"], 2), d1=pct(r["r1d"], 2), r1m=pct(r["r1m"]), r3m=pct(r["r3m"]),
                          r12m=pct(r["r12m"]), vs50=pct(r["pct_vs_50"]), vs200=pct(r["pct_vs_200"]),
                          offHigh=pct(r["off_high"]), rsi=round(r["rsi"]), adx=round(r["adx"]),
                          bull=r["pdi"] > r["mdi"], trend=trend))

    themes = [dict(label=lab, **e) for sym, lab in THEME_ETF.items() if (e := etf_stats(sym))]
    themes.sort(key=lambda x: (x["r3m"] or 0), reverse=True)

    # ---- sectors
    liquid = df[(df.dollar_vol > 20e6) & ~df.pinned]
    sectors = []
    for sec, g in df.groupby("sector"):
        lg = liquid[liquid.sector == sec].sort_values("score", ascending=False)
        lead = lg[lg.setup.isin(["Leader", "Extended"])].head(6)
        sectors.append(clean(dict(
            name=sec, etf=SECTOR_ETF.get(sec), etfStats=etf_stats(SECTOR_ETF[sec]) if sec in SECTOR_ETF else None,
            n=len(g), above50=pct((g.close > g.sma50).mean(), 0), above200=pct((g.close > g.sma200).mean(), 0),
            leaders=pct((g.tt_score >= 7).mean(), 0), medianRS=int(g.rs_rating.median()),
            r1m=pct(g.r1m.median()), r3m=pct(g.r3m.median()), score=round(float(g.score.median()), 1),
            topSyms=list(lead.symbol))))
    sectors.sort(key=lambda s: s["score"], reverse=True)

    # ---- industries
    ind = df.groupby("industry").agg(n=("symbol", "size"), score=("score", "median"), rs=("rs_rating", "median"),
                                     above200=("pct_vs_200", lambda s: (s > 0).mean()), r3m=("r3m", "median"),
                                     sector=("sector", "first")).query("n >= 4").sort_values("score", ascending=False)
    ind_rows = lambda x: [clean(dict(name=i, sector=r.sector, n=int(r.n), score=round(r.score, 1), rs=int(r.rs),
                                     above200=pct(r.above200, 0), r3m=pct(r.r3m))) for i, r in x.iterrows()]
    industries = dict(top=ind_rows(ind.head(10)), bottom=ind_rows(ind.tail(10).iloc[::-1]))

    # ---- candidate pools
    cand = (liquid[liquid.setup.isin(["Leader", "Extended", "Emerging"])]
            .sort_values("score", ascending=False).groupby("sector").head(10))
    watch = (liquid[(liquid.rs_rating >= 85) & liquid.off_high.between(-0.45, -0.12) & (liquid.close < liquid.sma50)]
             .sort_values("rs_rating", ascending=False).head(15))
    mega = df[df.yf.isin(MEGA)].sort_values("score", ascending=False)

    fund_syms = sorted(set(cand.yf) | set(watch.yf) | set(mega.yf))
    log("fundamentals for", len(fund_syms))
    with ThreadPoolExecutor(max_workers=8) as ex:
        funds = {f["sym"]: f for f in ex.map(fundamentals, fund_syms)}

    rowmap = lambda frame: [stock_row(r, funds.get(r.yf)) for _, r in frame.iterrows()]
    technicals = clean(dict(
        asOf=as_of, generatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        universe=dict(n=int(len(df)), sp500=int((df["index"] == "SP500").sum()), sp400=int((df["index"] == "SP400").sum())),
        tape=tape, market=market, sectors=sectors, industries=industries, themes=themes,
        candidates=rowmap(cand), watchCandidates=rowmap(watch), megacaps=rowmap(mega),
    ))
    (out_dir / "technicals.json").write_text(json.dumps(technicals, indent=1))
    (out_dir / "funds.json").write_text(json.dumps(clean(funds)))
    (out_dir / "digest.md").write_text(digest(technicals))
    log("wrote", out_dir)
    print(as_of)


# ---------------------------------------------------------------- digest for the news step
def digest(t):
    f = lambda v, suf="%": "n/a" if v is None else f"{v:+.1f}{suf}" if suf == "%" else f"{v}{suf}"
    L = [f"# Technical digest, close of {t['asOf']}", ""]
    s = t["market"]["spx"]
    b = t["market"]["breadth"]
    L += [f"S&P 500 {s['close']} ({f(s['d1'])} today, {f(s['offHigh'])} from 52-wk high, YTD {f(s['ytd'])}).",
          f"Breadth (S&P 500): {b['sp500']['above50']}% above 50-day, {b['sp500']['above200']}% above 200-day. "
          f"MidCap 400: {b['sp400']['above50']}% / {b['sp400']['above200']}%.", "", "## Tape"]
    L += [f"- {x['label']}: {x['value']} ({f(x['d1'])} 1d, {f(x['r1m'])} 1m)" for x in t["tape"]]
    L += ["", "## Sectors (median composite score; % above 50d / 200d; median 1m / 3m; top technical names)"]
    for x in t["sectors"]:
        L.append(f"- {x['name']} ({x['etf']}): score {x['score']}, {x['above50']}%/{x['above200']}%, "
                 f"{f(x['r1m'])}/{f(x['r3m'])}, leaders: {', '.join(x['topSyms']) or 'none'}")
    L += ["", "## Theme ETFs (3m return, vs 200d, trend)"]
    L += [f"- {x['sym']} {x['label']}: {f(x['r3m'])}, {f(x['vs200'])} vs 200d, {x['trend']}" for x in t["themes"]]
    L += ["", "## Strongest industries"] + [f"- {x['name']}: score {x['score']}, {x['above200']}% above 200d" for x in t["industries"]["top"]]
    L += ["", "## Weakest industries"] + [f"- {x['name']}: score {x['score']}, {x['above200']}% above 200d" for x in t["industries"]["bottom"]]

    def row(x):
        a = x.get("analysts") or {}
        up = a.get("upside")
        return (f"- {x['sym']} ({x['name']}; {x['industry']}): {x['setup']}, score {x['score']}, RS {x['rs']}, RSI {x['rsi']}, "
                f"{f(x['vs50'])} vs 50d, {f(x['offHigh'])} from high, risk line {x['risk']} ({f(x['riskPct'])}), "
                f"earnings {x.get('earnings') or 'n/a'}, analysts {a.get('rating') or 'n/a'}"
                f"{'' if up is None else f' (target {up:+.0f}%)'}{' [PINNED - likely pending deal, do not pick]' if x.get('pinned') else ''}")

    L += ["", "## Candidate pool by sector (pick from here; symbols outside the pool are allowed only if listed elsewhere in this digest)"]
    for sec in [x["name"] for x in t["sectors"]]:
        L.append(f"### {sec}")
        L += [row(x) for x in t["candidates"] if x["sector"] == sec]
    L += ["", "## Fallen leaders (high RS, below 50-day): watchlist pool"] + [row(x) for x in t["watchCandidates"]]
    L += ["", "## Mega caps"] + [row(x) for x in t["megacaps"]]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
