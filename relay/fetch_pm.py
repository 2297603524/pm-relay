#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kalshi 取数（在 GitHub Actions 里跑，境外网络）。

用法：
    python3 fetch_pm.py prices    # 事件 + 当前概率 → data/pm.json
    python3 fetch_pm.py history   # 合约走势        → data/history/<ticker>.json

为什么换成 Kalshi：Polymarket 的 gamma/clob 对机房 IP 有拦截
（GitHub 的 runner 直接被拒），Kalshi 是 CFTC 监管的交易所，公共读接口开放。

只依赖标准库。接口：
    /trade-api/v2/events                                事件元数据（category/title）
    /trade-api/v2/markets?event_ticker=                 该事件下的合约与报价
    /trade-api/v2/series/{s}/markets/{t}/candlesticks   历史 K 线
"""

import calendar
import json
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "liquidity-radar-relay/1.0 (+https://github.com/2297603524/pm-relay)",
      "Accept": "application/json"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
HIST = os.path.join(DATA, "history")

# Kalshi 的 category → 我们的类别
CAT_MAP = {
    "economics": "macro", "financials": "rates", "fed": "fomc", "politics": "macro",
    "companies": "equity", "climate": "commodity", "commodities": "commodity",
    "world": "trade", "science and technology": "equity",
}

MAX_EVENTS = 36          # 最多保留多少个事件
MAX_MARKETS = 6          # 每个事件最多取几个合约
MAX_HISTORY = 40         # 历史只抓成交量前 N 个合约


def _dbg(msg):
    """把进度写到 data/_debug.txt，失败时也能从仓库里看到"""
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "_debug.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + "  " + str(msg) + "\n")
    except Exception:
        pass


def get(url, timeout=20, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "ignore")
            return json.loads(raw)
        except Exception as e:
            last = e
            _dbg("GET %s -> %s" % (url, str(e)[:120]))
            time.sleep(1.2 * (i + 1))
    raise RuntimeError("GET failed %s: %s" % (url, last))


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def ts(iso):
    if not iso:
        return None
    try:
        return int(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return None


def cat_of(ev):
    return CAT_MAP.get((ev.get("category") or "").strip().lower(), "macro")


def mid_price(m):
    """买一/卖一的中点；没有报价就退到最新成交价"""
    bid = fnum(m.get("yes_bid_dollars"))
    ask = fnum(m.get("yes_ask_dollars"))
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    last = fnum(m.get("last_price_dollars"))
    return round(last, 4) if last > 0 else None


def fetch_events():
    d = get(BASE + "/events?status=open&limit=%d&with_nested_markets=false" % MAX_EVENTS)
    evs = d.get("events") or []
    _dbg("events=%d" % len(evs))
    out = []
    for ev in evs:
        ticker = ev.get("event_ticker") or ""
        if not ticker:
            continue
        try:
            md = get(BASE + "/markets?event_ticker=%s&limit=200" % urllib.parse.quote(ticker))
        except Exception as e:
            _dbg("markets %s -> %s" % (ticker, str(e)[:100]))
            continue
        rows = []
        for m in (md.get("markets") or [])[:MAX_MARKETS]:
            if m.get("status") not in ("active", "open", "initialized"):
                continue
            p = mid_price(m)
            if p is None or p <= 0:
                continue
            rows.append({
                "id": m.get("ticker") or "",
                "label": m.get("yes_sub_title") or m.get("subtitle") or "",
                "question": m.get("title") or "",
                "rules": (m.get("rules_primary") or "")[:1200],
                "price": p,
                "change": None,
                "volume": fnum(m.get("volume_24h_fp") or m.get("volume_fp")),
                "status": "open",
                "payout": None,
                "end": ts(m.get("close_time")),
            })
        if not rows:
            continue
        out.append({
            "id": ticker,
            "slug": ticker,
            "title": ev.get("title") or ticker,
            "original": ev.get("title") or "",
            "category": cat_of(ev),
            "status": "open",
            "end": rows[0]["end"],
            "volume": sum(r["volume"] for r in rows),
            "note": "",
            "checked_at": int(time.time()),
            "rows": rows,
        })
    return out


def cmd_prices():
    events = fetch_events()
    payload = {
        "at": int(time.time()),
        "partial": False,
        "review": {"at": 0, "summary": ""},
        "events": events,
        "source": "kalshi",
    }
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "pm.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    n = sum(len(e["rows"]) for e in events)
    _dbg("prices done events=%d contracts=%d" % (len(events), n))
    print("events=%d contracts=%d" % (len(events), n))


def series_of(ticker):
    """KXFEDDECISION-26DEC-C25 → KXFEDDECISION"""
    return (ticker or "").split("-")[0]


def candles(series, ticker, start, end, interval):
    url = "%s/series/%s/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=%d" % (
        BASE, urllib.parse.quote(series), urllib.parse.quote(ticker), start, end, interval)
    d = get(url)
    pts = []
    for c in d.get("candlesticks") or []:
        t = c.get("end_period_ts")
        pr = (c.get("price") or {}).get("close")
        if t and pr is not None:
            v = float(pr) / 100.0
            if v > 0:
                pts.append([int(t), round(v, 4)])
    return pts


def cmd_history():
    os.makedirs(HIST, exist_ok=True)
    path = os.path.join(DATA, "pm.json")
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    items = []
    for e in d.get("events") or []:
        for r in e.get("rows") or []:
            items.append((r.get("volume") or 0, r.get("id")))
    items.sort(reverse=True)
    ids = [i for _, i in items[:MAX_HISTORY] if i]
    _dbg("history start contracts=%d" % len(ids))
    now = int(time.time())
    ok = 0
    for n, tk in enumerate(ids, 1):
        try:
            s = series_of(tk)
            minute = candles(s, tk, now - 86400, now, 1)
            week = candles(s, tk, now - 7 * 86400, now, 60)
            full = candles(s, tk, now - 60 * 86400, now, 1440)
            doc = {
                "minute": minute, "minute_at": now, "minute_end": now,
                "week": week, "month": full, "month_step": 1800,
                "all": full, "all_at": now, "all_end": now,
                "revision": ["open", now],
            }
            with open(os.path.join(HIST, "%s.json" % tk), "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
            ok += 1
        except Exception as e:
            _dbg("skip %s: %s" % (tk, str(e)[:90]))
        if n % 10 == 0:
            _dbg("  %d/%d" % (n, len(ids)))
    _dbg("history done %d/%d" % (ok, len(ids)))
    print("history written:", ok)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "prices"
    try:
        if mode == "history":
            cmd_history()
        else:
            cmd_prices()
    except Exception:
        _dbg("FATAL " + traceback.format_exc()[-1500:])
        # 故意不抛出：让后面的 Commit 步骤仍然执行，把 _debug.txt 提交回来
        print("failed, see data/_debug.txt")
