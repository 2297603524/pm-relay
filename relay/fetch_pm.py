#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Polymarket 取数（在 GitHub Actions 里跑，境外网络）。

用法：
    python3 fetch_pm.py prices    # 抓事件 + 当前概率 → data/pm.json
    python3 fetch_pm.py history   # 抓合约走势       → data/history/<id>.json

只依赖标准库。所有接口都是 Polymarket 的公开只读接口：
    gamma-api.polymarket.com/events     事件与市场元数据（含 oneDayPriceChange）
    clob.polymarket.com/prices-history  单个 token 的历史价格
"""

import calendar
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "liquidity-radar-relay/1.0 (+https://github.com/)"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
HIST = os.path.join(DATA, "history")

# Polymarket 的 tag slug → 我们的类别
TAG_CAT = {
    "fed": "fomc", "fed-rates": "fomc", "interest-rates": "rates", "rates": "rates",
    "economy": "macro", "inflation": "macro", "macro": "macro", "economic-policy": "macro",
    "trade": "trade", "tariffs": "trade",
    "commodities": "commodity", "oil": "commodity", "gold": "commodity",
    "stocks": "equity", "equities": "equity", "business": "equity",
}

MAX_EVENTS = 60          # 最多保留多少个事件
MAX_HISTORY = 80         # 历史只抓成交额前 N 个合约，避免一次几百个请求


def get(url, timeout=25, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                       # 网络抖动重试
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("GET %s failed: %s" % (url, last))


def jlist(v):
    """gamma 里 outcomes / outcomePrices / clobTokenIds 都是 JSON 字符串"""
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
    except Exception:
        return []


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def cat_of(event):
    for tag in event.get("tags") or []:
        slug = (tag.get("slug") or "").lower()
        if slug in TAG_CAT:
            return TAG_CAT[slug]
    return "macro"


def fetch_events():
    url = ("%s/events?active=true&closed=false&limit=%d"
           "&order=volume24hr&ascending=false" % (GAMMA, MAX_EVENTS))
    evs = get(url)
    out = []
    now = int(time.time())
    for e in evs:
        rows = []
        for m in e.get("markets") or []:
            if m.get("closed"):
                continue
            prices = jlist(m.get("outcomePrices"))
            outs = jlist(m.get("outcomes"))
            if not prices:
                continue
            yes = fnum(prices[0])
            label = m.get("groupItemTitle") or (outs[0] if outs else "")
            chg = m.get("oneDayPriceChange")
            rows.append({
                "id": str(m.get("id") or ""),
                "label": label or "是",
                "question": m.get("question") or "",
                "rules": (m.get("description") or "")[:1200],
                "price": round(yes, 4),
                "change": None if chg is None else round(fnum(chg), 4),
                "volume": fnum(m.get("volumeNum") or m.get("volume")),
                "status": "open" if not m.get("closed") else "resolved",
                "payout": None if not m.get("closed") else (1 if yes >= 0.99 else 0),
                "end": int(calendar.timegm(time.strptime(m["endDate"][:19], "%Y-%m-%dT%H:%M:%S"))) if m.get("endDate") else e.get("endDate"),
            })
        if not rows:
            continue
        out.append({
            "id": str(e.get("id") or ""),
            "slug": e.get("slug") or "",
            "title": e.get("title") or "",
            "original": e.get("title") or "",
            "category": cat_of(e),
            "status": "open",
            "end": int(calendar.timegm(time.strptime(e["endDate"][:19], "%Y-%m-%dT%H:%M:%S"))) if e.get("endDate") else None,
            "volume": fnum(e.get("volume")),
            "note": "",
            "checked_at": now,
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
        "source": "polymarket-gamma",
    }
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "pm.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    n = sum(len(e["rows"]) for e in events)
    print("events=%d contracts=%d -> %s" % (len(events), n, path))


def pick_contracts():
    """按成交额取前 N 个合约 id，历史只抓这些"""
    path = os.path.join(DATA, "pm.json")
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    items = []
    for e in d.get("events") or []:
        for r in e.get("rows") or []:
            items.append((r.get("volume") or 0, r.get("id")))
    items.sort(reverse=True)
    return [i for _, i in items[:MAX_HISTORY] if i]


def token_of(mid):
    """市场 id → YES token id（clobTokenIds 第一个）"""
    d = get("%s/markets?id=%s" % (GAMMA, urllib.parse.quote(mid)))
    if isinstance(d, list) and d:
        ids = jlist(d[0].get("clobTokenIds"))
        if ids:
            return str(ids[0])
    return None


def prices_history(token, interval, fidelity):
    url = "%s/prices-history?market=%s&interval=%s&fidelity=%d" % (
        CLOB, urllib.parse.quote(token), interval, fidelity)
    d = get(url)
    pts = []
    for p in d.get("history") or []:
        t, v = p.get("t"), p.get("p")
        if t is not None and v is not None:
            pts.append([int(t), round(float(v), 4)])
    return pts


def cmd_history():
    os.makedirs(HIST, exist_ok=True)
    ids = pick_contracts()
    print("contracts to fetch:", len(ids))
    ok = 0
    for n, mid in enumerate(ids, 1):
        try:
            tok = token_of(mid)
            if not tok:
                continue
            minute = prices_history(tok, "1d", 5)
            week = prices_history(tok, "1w", 30)
            full = prices_history(tok, "max", 180)
            doc = {
                "minute": minute, "minute_at": int(time.time()), "minute_end": int(time.time()),
                "week": week, "month": full, "month_step": 1800,
                "all": full, "all_at": int(time.time()), "all_end": int(time.time()),
                "revision": ["open", int(time.time())],
            }
            with open(os.path.join(HIST, "%s.json" % mid), "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
            ok += 1
        except Exception as e:
            print("skip %s: %s" % (mid, str(e)[:80]))
        if n % 10 == 0:
            print("  %d/%d" % (n, len(ids)))
    print("history written:", ok)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "prices"
    if mode == "history":
        cmd_history()
    else:
        cmd_prices()
