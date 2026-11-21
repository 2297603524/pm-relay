#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kalshi 取数（在 GitHub Actions 里跑，境外网络）。

用法：
    python3 fetch_pm.py prices    # 事件 + 当前概率 → data/pm.json
    python3 fetch_pm.py history   # 合约走势        → data/history/<ticker>.json

为什么用 Kalshi：Polymarket 的 gamma/clob 对机房 IP 有拦截（GitHub runner 直接被拒），
Kalshi 是 CFTC 监管的交易所，公共读接口开放，宏观系列有真实成交。

为什么按「系列」抓：/markets 的全量目录被体育串关占满，翻页根本翻不到宏观市场；
而 /events?series_ticker=...&with_nested_markets=true 一次就能带回该系列所有合约
和买卖价、成交量。所以这里用系列白名单，命中宏观/利率类。

只依赖标准库。Kalshi 限流较紧：串行 + 间隔 0.35s，429 按 Retry-After 退避。
"""

import calendar
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "liquidity-radar-relay/1.0 (+https://github.com/2297603524/pm-relay)",
      "Accept": "application/json"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
HIST = os.path.join(DATA, "history")

# 系列名里出现这些词就算宏观/利率类
KEEP_WORDS = ("FED", "FOMC", "CPI", "PCE", "GDP", "PAYROLL", "UNEMPLOY",
              "INFLATION", "RECESSION", "RATE", "TARIFF", "JOBS", "RETAIL")

MAX_SERIES = 18          # 最多抓多少个系列
MAX_EVENTS = 24          # 最终保留多少个事件
MAX_MARKETS = 6          # 每个事件最多取几个合约
MAX_HISTORY = 20         # 历史只抓成交量前 N 个合约
PACE = 0.35              # 请求间隔（秒）

_DBG_STARTED = [False]


def _dbg(msg):
    try:
        os.makedirs(DATA, exist_ok=True)
        mode = "w" if not _DBG_STARTED[0] else "a"
        _DBG_STARTED[0] = True
        with open(os.path.join(DATA, "_debug.txt"), mode, encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + "  " + str(msg) + "\n")
    except Exception:
        pass


def get(url, timeout=20, tries=5):
    last = None
    for i in range(tries):
        try:
            time.sleep(PACE)
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                wait = 0
                try:
                    wait = int(e.headers.get("Retry-After") or 0)
                except Exception:
                    wait = 0
                wait = max(wait, 4 * (i + 1))
                _dbg("429 -> sleep %ds" % wait)
                time.sleep(wait)
                continue
            _dbg("HTTP %s %s" % (e.code, url.split("/v2")[-1][:70]))
            time.sleep(1.5 * (i + 1))
        except Exception as e:
            last = e
            _dbg("ERR %s" % str(e)[:90])
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("GET failed: %s" % last)


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


def mid_price(m):
    bid = fnum(m.get("yes_bid_dollars"))
    ask = fnum(m.get("yes_ask_dollars"))
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    last = fnum(m.get("last_price_dollars"))
    return round(last, 4) if last > 0 else None


def cat_of(title):
    t = (title or "").lower()
    if "fed" in t or "fomc" in t or "interest rate" in t:
        return "fomc"
    if "cpi" in t or "pce" in t or "gdp" in t or "payroll" in t or "unemploy" in t or "inflation" in t:
        return "macro"
    if "tariff" in t or "trade" in t:
        return "trade"
    if "oil" in t or "gold" in t or "copper" in t:
        return "commodity"
    if "rate" in t:
        return "rates"
    return "macro"


def macro_series():
    """拿 Economics 系列的 ticker 列表；失败则用内置白名单兜底"""
    out = []
    try:
        d = get(BASE + "/series?category=Economics")
        for s in d.get("series") or []:
            t = (s.get("ticker") or "").upper()
            if t and any(w in t for w in KEEP_WORDS):
                out.append(t)
    except Exception as e:
        _dbg("series list failed: %s" % str(e)[:80])
    if not out:
        out = ["KXFEDDECISION", "KXFED", "KXCPIYOY", "KXPAYROLLS", "KXGDPYOY",
               "KXRECSSNBER", "KXUNEMPLOYMENT", "KXTARIFFS"]
        _dbg("series fallback")
    # 去重 + 限制数量
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    _dbg("series=%d %s" % (len(uniq), ",".join(uniq[:MAX_SERIES])))
    return uniq[:MAX_SERIES]


def fetch_series_events(series):
    """一个系列 → 其下所有 open 事件（带嵌套合约与报价）"""
    d = get(BASE + "/events?status=open&series_ticker=%s&with_nested_markets=true"
            % urllib.parse.quote(series))
    out = []
    for ev in d.get("events") or []:
        rows = []
        for m in ev.get("markets") or []:
            if m.get("status") not in ("active", "open", "initialized"):
                continue
            vol = fnum(m.get("volume_fp"))
            vol24 = fnum(m.get("volume_24h_fp"))
            p = mid_price(m)
            if p is None or p <= 0:
                continue
            if vol <= 0 and vol24 <= 0:
                continue                      # 完全没成交的僵尸合约不要
            rows.append({
                "id": m.get("ticker") or "",
                "label": m.get("yes_sub_title") or m.get("subtitle") or "",
                "question": m.get("title") or "",
                "rules": (m.get("rules_primary") or "")[:1200],
                "price": p,
                "change": None,
                "volume": vol24 or vol,
                "status": "open",
                "payout": None,
                "end": ts(m.get("close_time")),
            })
        if not rows:
            continue
        rows.sort(key=lambda r: r["volume"], reverse=True)
        rows = rows[:MAX_MARKETS]
        title = ev.get("title") or rows[0]["question"] or ev.get("event_ticker")
        ends = [r["end"] for r in rows if r["end"]]
        out.append({
            "id": ev.get("event_ticker") or "",
            "slug": ev.get("event_ticker") or "",
            "title": title,
            "original": title,
            "category": cat_of(title),
            "status": "open",
            "end": min(ends) if ends else None,
            "volume": round(sum(r["volume"] for r in rows), 2),
            "note": "",
            "checked_at": int(time.time()),
            "rows": rows,
        })
    return out


def fetch_events():
    built = []
    for s in macro_series():
        try:
            built.extend(fetch_series_events(s))
        except Exception as e:
            _dbg("series %s failed: %s" % (s, str(e)[:80]))
    _dbg("events built=%d" % len(built))
    built.sort(key=lambda e: e["volume"], reverse=True)
    return built[:MAX_EVENTS]


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
    return (ticker or "").split("-")[0]


def candle_points(doc):
    pts = []
    for c in doc.get("candlesticks") or []:
        t = c.get("end_period_ts")
        if not t:
            continue
        v = None
        pr = (c.get("price") or {}).get("close")
        if pr:
            v = float(pr) / 100.0
        if not v:
            bid = ((c.get("yes_bid") or {}).get("close") or 0)
            ask = ((c.get("yes_ask") or {}).get("close") or 0)
            if bid and ask:
                v = (float(bid) + float(ask)) / 200.0
        if v and v > 0:
            pts.append([int(t), round(v, 4)])
    return pts


def candles(series, ticker, start, end, interval):
    url = "%s/series/%s/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=%d" % (
        BASE, urllib.parse.quote(series), urllib.parse.quote(ticker), start, end, interval)
    return candle_points(get(url))


def cmd_history():
    os.makedirs(HIST, exist_ok=True)
    with open(os.path.join(DATA, "pm.json"), encoding="utf-8") as f:
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
            if not (minute or week or full):
                _dbg("empty %s" % tk)
                continue
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
            _dbg("skip %s: %s" % (tk, str(e)[:80]))
        if n % 5 == 0:
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
