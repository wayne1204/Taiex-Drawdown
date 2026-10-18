#!/usr/bin/env python3
"""
Single-stock monitor - data fetcher

Builds one daily series per stock on the watchlist (stocks.txt) from five
public sources:

  1. Daily OHLC + volume       TWSE STOCK_DAY           (per stock, monthly query)
  2. Institutional net         TWSE T86                 (whole market, single day)
  3. Margin balance / change   TWSE MI_MARGN ALL        (whole market, single day)
  4. Shareholder distribution  norway.twsthr.info       (per stock, weekly history)
  5. Main-force net + brokers  fubon-ebrokerdj (DJ)     (per stock, ~45 sessions)

The two whole-market daily queries are shared by every stock on the list, and
are cached slimmed down to the few columns read back (see slim_t86 / slim_margin)
so a year of them stays a few MB. Adding a stock to the watchlist therefore
costs only its own K-bar months plus two page loads.

The main-force series from DJ only reaches back ~45 sessions. Every run folds
it into the previous output, so history accumulates from the first run onward.

Outputs:
  data/stocks/<code>.json   one stock
  data/stocks/<code>.js     same, as window.STOCK_DATA[code] (works over file://)
  data/stocks/index.js      window.STOCK_LIST, the watchlist the page offers

Usage:
  python3 fetch_stocks.py                     # every stock in stocks.txt
  python3 fetch_stocks.py --stocks 2327,2330  # just these
  python3 fetch_stocks.py --months 24         # deeper history (default 12)
  python3 fetch_stocks.py --rebuild           # no requests; cache only

Only TWSE-listed stocks are supported: TPEx (OTC) stocks are not in STOCK_DAY,
T86 or MI_MARGN, and the run refuses them rather than emit a half-empty series.

Note: Chinese string literals below are matched against response payloads
(column headers, page labels). They are data and must not be translated.
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime

import fetch_data as fd

ROOT = fd.ROOT
OUT_DIR = os.path.join(fd.DATA_DIR, "stocks")
WATCHLIST = os.path.join(ROOT, "stocks.txt")
DEFAULT_MONTHS = 12

HOLDERS_URL = "https://norway.twsthr.info/StockHolders.aspx?stock={code}"
DJ_BASE = "https://fubon-ebrokerdj.fbs.com.tw"
DJ_SERIES_URL = DJ_BASE + "/Z/ZC/ZCO/CZCO.DJBCD?A={code}"
# zco_<code>.djhtm is the one-day table; the _N suffix picks the window
# from the page's own <select name="D">: 2 = 5 days, 4 = 20, 6 = 60.
DJ_BROKER_PERIODS = {"1": "", "5": "_2", "20": "_4", "60": "_6"}
DJ_BROKER_URL = DJ_BASE + "/z/zc/zco/zco_{code}{suffix}.djhtm"

LOTS = 1000  # shares per lot (張)


# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------

def read_watchlist(path=WATCHLIST):
    """Stock codes from stocks.txt: one per line, `#` starts a comment."""
    if not os.path.exists(path):
        return []
    codes = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            code = line.split("#", 1)[0].strip()
            if code and code not in codes:
                codes.append(code)
    return codes


# --------------------------------------------------------------------------
# TWSE: whole-market daily tables, slimmed before caching
# --------------------------------------------------------------------------

def _col(fields, test):
    for i, name in enumerate(fields):
        if test(str(name)):
            return i
    return None


def slim_t86(payload):
    """
    T86 -> {"rows": {code: [foreign, trust, dealer]}} in shares.

    Columns are located by header name, not position: TWSE inserted the
    外資自營商 columns in late 2017, shifting everything after them. Foreign
    is 外陸資 + 外資自營商, matching the market-level series in fetch_data.py.
    """
    fields = payload.get("fields") or []
    fgn = _col(fields, lambda f: f.startswith("外陸資買賣超") or f.startswith("外資買賣超"))
    fgn_dealer = _col(fields, lambda f: f.startswith("外資自營商買賣超"))
    trust = _col(fields, lambda f: f.startswith("投信買賣超"))
    dealer = _col(fields, lambda f: f.startswith("自營商買賣超") and "(" not in f)
    rows = {}
    for r in payload.get("data") or []:
        code = str(r[0]).strip()

        def num(i):
            return fd.parse_num(r[i]) if i is not None and i < len(r) else None

        f = num(fgn)
        fdlr = num(fgn_dealer)
        if f is not None and fdlr is not None:
            f += fdlr
        rows[code] = [f, num(trust), num(dealer)]
    return {"stat": "OK", "rows": rows}


def slim_margin(payload):
    """MI_MARGN (selectType=ALL) -> {"rows": {code: [prev_balance, balance]}} in lots."""
    rows = {}
    for t in payload.get("tables") or []:
        fields = t.get("fields") or []
        if not fields or str(fields[0]).strip() != "代號":
            continue
        # 融資 comes first: 買進 賣出 現金償還 前日餘額 今日餘額 ...
        prev_i = _col(fields, lambda f: f == "前日餘額")
        bal_i = _col(fields, lambda f: f == "今日餘額")
        for r in t.get("data") or []:
            if prev_i is None or bal_i is None or len(r) <= bal_i:
                continue
            rows[str(r[0]).strip()] = [fd.parse_num(r[prev_i]), fd.parse_num(r[bal_i])]
    return {"stat": "OK", "rows": rows}


def fetch_t86_day(ymd, rebuild_only=False):
    p = fd.fetch("T86", "fund/T86",
                 {"date": ymd, "selectType": "ALLBUT0999", "response": "json"},
                 ymd, rebuild_only, slim=slim_t86)
    return (p or {}).get("rows") or {}


def fetch_margin_all_day(ymd, rebuild_only=False):
    p = fd.fetch("MI_MARGN_ALL", "marginTrading/MI_MARGN",
                 {"date": ymd, "selectType": "ALL", "response": "json"},
                 ymd, rebuild_only, slim=slim_margin)
    return (p or {}).get("rows") or {}


def parse_stock_day(payload):
    """STOCK_DAY -> (name, {date: {open, high, low, close, volume}}); volume in lots."""
    out = {}
    for row in fd.rows_of(payload):
        if len(row) < 7:
            continue
        d = fd.parse_twse_date(row[0])
        if not d:
            continue
        shares = fd.parse_num(row[1])
        out[d] = {
            "open": fd.parse_num(row[3]), "high": fd.parse_num(row[4]),
            "low": fd.parse_num(row[5]), "close": fd.parse_num(row[6]),
            "volume": round(shares / LOTS) if shares is not None else None,
        }
    # Title: "115年10月 2327 國巨*            各日成交資訊"
    name = None
    m = re.search(r"\d+年\d+月\s+\S+\s+(.+?)\s+各日成交資訊", (payload or {}).get("title", ""))
    if m:
        name = m.group(1).strip()
    return name, out


def fetch_stock_month(code, ym, rebuild_only=False):
    p = fd.fetch(f"STOCK_DAY/{code}", "afterTrading/STOCK_DAY",
                 {"date": f"{ym}01", "stockNo": code, "response": "json"},
                 ym, rebuild_only)
    return parse_stock_day(p)


# --------------------------------------------------------------------------
# Third-party pages (shareholders, main force)
# --------------------------------------------------------------------------

def fetch_page(source, key, url, encoding, rebuild_only=False):
    """
    GET a third-party page, cached for the day it was fetched on. These pages
    always show "latest", so there is no closed period to cache forever; one
    fetch per day per page is all a daily job needs. Returns text or None.
    A failed fetch falls back to the last copy on disk.
    """
    cp = fd._cache_path(source, key)
    cached = None
    if os.path.exists(cp):
        with open(cp, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if rebuild_only or cached.get("_fetchedOn") == date.today().isoformat():
            fd._stats["cache_hits"] += 1
            return cached.get("body")
    if rebuild_only:
        return None

    delay, last_err = 2.0, None
    for attempt in range(fd.MAX_RETRIES):
        try:
            fd._stats["requests"] += 1
            r = fd.SESSION.get(url, timeout=30, headers={"Referer": url})
            r.raise_for_status()
            body = r.content.decode(encoding, errors="replace")
            with open(cp, "w", encoding="utf-8") as f:
                json.dump({"_fetchedOn": date.today().isoformat(), "url": url,
                           "body": body}, f, ensure_ascii=False)
            time.sleep(fd.SLEEP_BASE)
            return body
        except Exception as e:  # noqa: BLE001 - same retry policy as fetch()
            last_err = e
            fd._stats["retries"] += 1
            if attempt < fd.MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2
    print(f"  !! {source} {key} failed: {last_err}", file=sys.stderr)
    return cached.get("body") if cached else None


def _cell_text(s):
    s = re.sub(r"<br\s*/?>|</br>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).replace("\xa0", " ").strip()


def html_rows(body):
    """Every <tr> as a list of cell texts. Tolerates the `<tr/>` both sites emit."""
    out = []
    for seg in re.split(r"<tr\b[^>]*>", body, flags=re.I)[1:]:
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", seg, flags=re.I | re.S)
        if cells:
            out.append([_cell_text(c) for c in cells])
    return out


# Header text (whitespace removed) -> output key
HOLDER_COLS = {
    "資料日期": "date",
    "總股東人數": "holders",
    "平均張數/人": "avgLots",
    ">400張大股東持有百分比": "big400Pct",
    ">400張大股東人數": "big400",
    ">1000張人數": "big1000",
    ">1000張大股東持有百分比": "big1000Pct",
}


def parse_holders(body):
    """
    twsthr StockHolders page -> weekly rows, oldest first:
    {date, holders, avgLots, big400, big400Pct, big1000, big1000Pct}.

    A row with a handful of holders is the depository's placeholder in the
    week of a share split or capital reduction (2327 shows 1 holder owning
    100%); it is dropped rather than plotted as a cliff.
    """
    if not body:
        return []
    m = re.search(r"id='Details'.*?</table>", body, flags=re.S)
    rows = html_rows(m.group(0) if m else body)
    idx = None
    out = []
    for cells in rows:
        keys = [re.sub(r"\s+", "", c) for c in cells]
        if "資料日期" in keys:
            idx = {HOLDER_COLS[k]: i for i, k in enumerate(keys) if k in HOLDER_COLS}
            continue
        if idx is None or "date" not in idx or len(cells) <= max(idx.values()):
            continue
        raw = re.sub(r"\D", "", cells[idx["date"]])
        if not re.fullmatch(r"\d{8}", raw):
            continue
        rec = {"date": fd.parse_twse_date(raw)}
        for k, i in idx.items():
            if k != "date":
                rec[k] = fd.parse_num(cells[i])
        if rec.get("holders") is None or rec["holders"] < 100:
            continue
        out.append(rec)
    return sorted(out, key=lambda r: r["date"])


def parse_main_series(body, trading_days):
    """
    CZCO.DJBCD -> {date: net lots}. The payload is three space-separated lists:
    MMDD dates, closes, main-force net. There is no year, so each MMDD is
    pinned to the latest unused trading day with that month/day, walking back
    from the newest - a point that matches no recent session is dropped.
    """
    if not body:
        return {}
    parts = body.strip().split()
    if len(parts) < 3:
        return {}
    mmdd = parts[0].split(",")
    nets = parts[2].split(",")
    if len(mmdd) != len(nets):
        return {}
    days = sorted(trading_days, reverse=True)
    out, j = {}, 0
    for md, v in zip(reversed(mmdd), reversed(nets)):
        for k in range(j, min(j + 10, len(days))):
            if days[k][5:7] + days[k][8:10] == md:
                n = fd.parse_num(v)
                if n is not None:
                    out[days[k]] = n
                j = k + 1
                break
    return out


def parse_brokers(body):
    """
    zco page -> {"updated", "buy": [...], "sell": [...]}, top brokers each side.
    Each entry: {name, buy, sell, net, pct}; lots, pct is % of the period's volume.
    """
    if not body:
        return None
    buy, sell = [], []
    for cells in html_rows(body):
        if len(cells) != 10 or not cells[1].replace(",", "").isdigit():
            continue
        for side, c in ((buy, cells[0:5]), (sell, cells[5:10])):
            if c[0]:
                side.append({"name": c[0], "buy": fd.parse_num(c[1]),
                             "sell": fd.parse_num(c[2]), "net": fd.parse_num(c[3]),
                             "pct": fd.parse_num(c[4].rstrip("%"))})
    m = re.search(r"最後更新日：(\d{4}/\d{1,2}/\d{1,2})", body)
    if not buy and not sell:
        return None
    return {"updated": fd.parse_twse_date(m.group(1)) if m else None,
            "buy": buy, "sell": sell}


# --------------------------------------------------------------------------
# Build one stock
# --------------------------------------------------------------------------

def load_previous(code):
    path = os.path.join(OUT_DIR, f"{code}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            prev = json.load(f)
        return prev if isinstance(prev, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_stock(code, months, market, rebuild_only=False):
    """market: {date: (t86_rows, margin_rows)} shared across stocks."""
    name, kbars = None, {}
    for ym in months:
        n, rows = fetch_stock_month(code, ym, rebuild_only)
        name = n or name
        kbars.update(rows)
    if not kbars:
        raise SystemExit(f"ERROR: no STOCK_DAY data for {code}. Is it TWSE-listed? "
                         "TPEx (OTC) stocks are not supported.")
    days = sorted(kbars)

    prev = load_previous(code)
    main = {r["date"]: r.get("mainForce") for r in prev.get("daily") or []
            if r.get("mainForce") is not None}
    main.update(parse_main_series(
        fetch_page("DJ_CZCO", code, DJ_SERIES_URL.format(code=code), "cp950", rebuild_only),
        days))

    daily = []
    for d in days:
        inst, mg = market.get(d, ({}, {}))
        f, t, dl = inst.get(code) or [None, None, None]
        prev_bal, bal = mg.get(code) or [None, None]
        lots = lambda v: round(v / LOTS) if v is not None else None  # noqa: E731
        daily.append({
            "date": d, **kbars[d],
            "foreign": lots(f), "trust": lots(t), "dealer": lots(dl),
            "marginBalance": bal,
            "marginChange": bal - prev_bal if bal is not None and prev_bal is not None else None,
            "mainForce": main.get(d),
        })

    holders = parse_holders(
        fetch_page("TWSTHR", code, HOLDERS_URL.format(code=code), "utf-8", rebuild_only))
    if not holders:
        holders = prev.get("holders") or []

    brokers = {}
    for label, suffix in DJ_BROKER_PERIODS.items():
        b = parse_brokers(fetch_page("DJ_ZCO", f"{code}{suffix or '_1'}",
                                     DJ_BROKER_URL.format(code=code, suffix=suffix),
                                     "cp950", rebuild_only))
        if b is None:
            b = (prev.get("brokers") or {}).get(label)
        if b is not None:
            brokers[label] = b

    return {
        "code": code, "name": name or prev.get("name") or code,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "start": days[0], "end": days[-1],
        "daily": daily, "holders": holders, "brokers": brokers,
    }


def write_stock(s):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"{s['code']}.json"), "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, f"{s['code']}.js"), "w", encoding="utf-8") as f:
        f.write("(window.STOCK_DATA=window.STOCK_DATA||{})[" + json.dumps(s["code"]) + "]="
                + json.dumps(s, ensure_ascii=False, separators=(",", ":")) + ";\n")


def write_index(codes):
    """The page's stock picker: every built stock on the watchlist, in list order."""
    entries = []
    for code in codes:
        s = load_previous(code)
        if s:
            entries.append({"code": code, "name": s.get("name"), "end": s.get("end")})
    with open(os.path.join(OUT_DIR, "index.js"), "w", encoding="utf-8") as f:
        f.write("window.STOCK_LIST=" + json.dumps(entries, ensure_ascii=False) + ";\n")
    return entries


def main():
    ap = argparse.ArgumentParser(description="Fetch single-stock data for stock.html")
    ap.add_argument("--stocks", help="comma-separated codes (default: stocks.txt)")
    ap.add_argument("--months", type=int, default=DEFAULT_MONTHS,
                    help=f"months of history (default {DEFAULT_MONTHS})")
    ap.add_argument("--rebuild", action="store_true",
                    help="issue no requests; rebuild outputs from the cache only")
    args = ap.parse_args()

    codes = [c.strip() for c in args.stocks.split(",")] if args.stocks else read_watchlist()
    codes = [c for c in codes if c]
    if not codes:
        raise SystemExit("ERROR: no stocks. Add codes to stocks.txt or pass --stocks.")
    for c in codes:
        if not re.fullmatch(r"[0-9A-Z]{4,6}", c):
            raise SystemExit(f"ERROR: {c!r} is not a stock code")

    today = date.today()
    end = f"{today.year:04d}-{today.month:02d}"
    y, m = today.year, today.month - (args.months - 1)
    while m < 1:
        y, m = y - 1, m + 12
    months = fd.month_range(f"{y:04d}-{m:02d}", end)
    t0 = time.time()
    print(f"[{'REBUILD' if args.rebuild else 'FETCH'}] {', '.join(codes)} "
          f"· {months[0]} -> {months[-1]}")

    # The K-bar months define the calendar for the shared whole-market queries.
    calendar = set()
    for code in codes:
        for ym in months:
            calendar.update(fetch_stock_month(code, ym, args.rebuild)[1])
    calendar = sorted(calendar)
    if not calendar:
        raise SystemExit("ERROR: no trading days retrieved; aborting.")
    print(f"  {len(calendar)} trading days; institutional + margin, two queries per day")

    market = {}
    for i, d in enumerate(calendar, 1):
        ymd = d.replace("-", "")
        market[d] = (fetch_t86_day(ymd, args.rebuild), fetch_margin_all_day(ymd, args.rebuild))
        if i % 25 == 0 or i == len(calendar):
            print(f"  {d}  {i}/{len(calendar)}  {(time.time() - t0) / 60:.1f} min", flush=True)

    for code in codes:
        s = build_stock(code, months, market, args.rebuild)
        write_stock(s)
        d = s["daily"]
        cov = lambda k: sum(1 for r in d if r[k] is not None)  # noqa: E731
        print(f"  {code} {s['name']}: {len(d)} days ({s['start']} -> {s['end']}) · "
              f"法人 {cov('foreign')} · 融資 {cov('marginBalance')} · "
              f"主力 {cov('mainForce')} · 股東週 {len(s['holders'])} · "
              f"分點 {','.join(s['brokers']) or '-'}")

    entries = write_index(list(dict.fromkeys(read_watchlist() + codes)))
    st = fd._stats
    print(f"  index: {len(entries)} stock(s) · {st['requests']} requests / "
          f"{st['cache_hits']} cache hits / {st['retries']} retries · "
          f"{(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
