#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kalshi 取数（在 GitHub Actions 里跑，境外网络）。

用法：
    python3 fetch_pm.py prices    # 事件 + 当前概率 → data/pm.json
    python3 fetch_pm.py history   # 合约走势        → data/history/<ticker>.json

为什么用 Kalshi：Polymarket 的 gamma/clob 对机房 IP 有拦截（GitHub runner 直接被拒），
Kalshi 是 CFTC 监管的交易所，公共读接口开放，有真实成交价。

策略：从**合约层**（/markets）翻页抓，按 24h 成交额分组到事件。
这样能直接拿到 close_time 和成交量，不必逐个事件请求，也避免抓到一批
到期在 2035 年、零成交的僵尸市场。

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

MAX_EVENTS = 24          # 最终保留多少个事件
MAX_MARKETS = 6          # 每个事件最多取几个合约
MAX_HISTORY = 20         # 历史只抓成交量前 N 个合约
PACE = 0.35              # 请求间隔（秒）
SCAN_PAGES = 8           # 合约层翻多少页（每页 200）

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


def scan_markets(pages=SCAN_PAGES):
    """翻页抓 open 合约，返回按 event_ticker 分组的字典"""
    groups, cursor, total = {}, None, 0
    for _ in range(pages):
        url = BASE + "/markets?status=open&limit=200"
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor)
        d = get(url)
        batch = d.get("markets") or []
        total += len(batch)
        for m in batch:
            et = m.get("event_ticker") or ""
            if not et or m.get("status") not in ("active", "open", "initialized"):
                continue
            p = mid_price(m)
            if p is None or p <= 0:
                continue
            m["_p"] = p
            m["_vol"] = fnum(m.get("volume_24h_fp") or m.get("volume_fp"))
            groups.setdefault(et, []).append(m)
        cursor = d.get("cursor")
        if not cursor:
            break
    _dbg("scanned markets=%d events=%d" % (total, len(groups)))
    return groups


def event_meta(ticker):
    """事件元数据；失败就返回空，不影响主流程"""
    try:
        d = get(BASE + "/events/" + urllib.parse.quote(ticker))
        return d.get("event") or {}
    except Exception:
        return {}


def cat_of(title, raw_cat):
    c = (raw_cat or "").lower()
    t = (title or "").lower()
    if "fed" in t or "fomc" in t or "interest rate" in t:
        return "fomc"
    if c in ("financials", "companies"):
        return "equity" if c == "companies" else "rates"
    if "cpi" in t or "inflation" in t or "gdp" in t or "payroll" in t or "unemployment" in t:
        return "macro"
    if "tariff" in t or "trade" in t:
        return "trade"
    if "oil" in t or "gold" in t or "copper" in t:
        return "commodity"
    return "macro"


def fetch_events():
    groups = scan_markets()
    scored = []
    for et, ms in groups.items():
        vol = sum(m["_vol"] for m in ms)
        scored.append((vol, et, ms))
    scored.sort(key=lambda x: x[0], reverse=True)

    out = []
    for vol, et, ms in scored[:MAX_EVENTS]:
        ms = sorted(ms, key=lambda m: m["_vol"], reverse=True)[:MAX_MARKETS]
        meta = event_meta(et)
        title = meta.get("title") or ms[0].get("title") or et
        rows = [{
            "id": m.get("ticker") or "",
            "label": m.get("yes_sub_title") or m.get("subtitle") or "",
            "question": m.get("title") or "",
            "rules": (m.get("rules_primary") or "")[:1200],
            "price": m["_p"],
            "change": None,
            "volume": m["_vol"],
            "status": "open",
            "payout": None,
            "end": ts(m.get("close_time")),
        } for m in ms]
        out.append({
            "id": et,
            "slug": et,
            "title": title,
            "original": title,
            "category": cat_of(title, meta.get("category")),
            "status": "open",
            "end": min([r["end"] for r in rows if r["end"]] or [None]) if any(r["end"] for r in rows) else None,
            "volume": round(vol, 2),
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
    return (ticker or "").split("-")[0]


def candle_points(doc):
    """K 线里取收盘价（分 → 概率）；没有成交就用买卖价中点"""
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
