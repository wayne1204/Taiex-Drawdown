#!/usr/bin/env python3
"""
Single-stock monitor - data fetcher

Builds a daily series for EVERY TWSE-listed stock and ETF (about 1,300), plus
richer broker data for the stocks on the watchlist (stocks.txt):

  Every stock
  1. Daily OHLC + volume       TWSE MI_INDEX            (whole market, single day)
  2. Institutional net         TWSE T86                 (whole market, single day)
  3. Margin balance / change   TWSE MI_MARGN ALL        (whole market, single day)
  4. Shareholder distribution  norway.twsthr.info       (per stock, weekly history)

  Watchlist stocks only (one request per stock per session - too many for all)
  5. Main-force net + brokers  fubon-ebrokerdj (DJ)     (series, daily tables, windows)

The three whole-market queries cost three requests a day however many stocks
are built; they are cached slimmed down to the columns read back. The trading
calendar comes from the TAIEX months fetch_data.py already caches. Shareholder
pages are refreshed once a week (TDCC publishes weekly), so a normal nightly
run is three TWSE requests plus the watchlist's broker pages; the first run
after each Saturday also walks every stock's shareholder page (~45 min).

Outputs:
  data/stocks/<code>.js     one stock, as window.STOCK_DATA[code] (works over file://)
  data/stocks/index.js      window.STOCK_LIST: every built stock, watchlist first

Usage:
  python3 fetch_stocks.py                     # whole market + watchlist extras
  python3 fetch_stocks.py --watchlist-only    # just stocks.txt (fast)
  python3 fetch_stocks.py --stocks 2327,2330  # just these, with broker data
  python3 fetch_stocks.py --months 24         # deeper history (default 12)
  python3 fetch_stocks.py --rebuild           # no requests; cache only

Only TWSE-listed securities are covered: TPEx (OTC) stocks are not in MI_INDEX,
T86 or MI_MARGN.

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
from datetime import date, datetime, timedelta

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
# Custom range (e = from, f = to, unpadded Y-M-D): one session's broker table
# for any past day. Past days never change, so these are cached for good.
DJ_BROKER_DAY_URL = DJ_BASE + "/z/zc/zco/zco.djhtm?a={code}&e={d}&f={d}"

LOTS = 1000  # shares per lot (張)

# Ordinary stocks (4 digits) and ETFs (00xx, 00xxx, 00xxxR ...). Excludes
# preferred shares (2881A), TDRs (91xxxx), ETNs (020xxx) and REIT certificates
# (01xxxT), which have no shareholder or institutional coverage worth a page.
UNIVERSE_RE = re.compile(r"\d{4}|00\d{2,4}[A-Z]?")


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


def slim_quotes(payload):
    """
    MI_INDEX (type=ALLBUT0999) -> {"rows": {code: [open, high, low, close, lots]},
    "names": {code: name}}. The quote table is found by its header, among the
    index and statistics tables that share the payload.
    """
    rows, names = {}, {}
    for t in payload.get("tables") or []:
        fields = [str(f) for f in t.get("fields") or []]
        if "證券代號" not in fields or "收盤價" not in fields:
            continue
        ix = {k: fields.index(k) for k in ("證券代號", "證券名稱", "成交股數",
                                            "開盤價", "最高價", "最低價", "收盤價")
              if k in fields}
        for r in t.get("data") or []:
            code = str(r[ix["證券代號"]]).strip()
            num = lambda k: fd.parse_num(r[ix[k]]) if k in ix else None  # noqa: E731
            shares = num("成交股數")
            rows[code] = [num("開盤價"), num("最高價"), num("最低價"), num("收盤價"),
                          round(shares / LOTS) if shares is not None else None]
            if "證券名稱" in ix:
                names[code] = str(r[ix["證券名稱"]]).strip()
    return {"stat": "OK", "rows": rows, "names": names}


def fetch_quotes_day(ymd, rebuild_only=False):
    p = fd.fetch("MI_INDEX", "afterTrading/MI_INDEX",
                 {"date": ymd, "type": "ALLBUT0999", "response": "json"},
                 ymd, rebuild_only, slim=slim_quotes)
    return (p or {}).get("rows") or {}, (p or {}).get("names") or {}


# --------------------------------------------------------------------------
# Third-party pages (shareholders, main force)
# --------------------------------------------------------------------------

def fetch_page(source, key, url, encoding, rebuild_only=False, permanent=False,
               fresh_since=None):
    """
    GET a third-party page, cached for the day it was fetched on. These pages
    always show "latest", so there is no closed period to cache forever; one
    fetch per day per page is all a daily job needs. Returns text or None.
    A failed fetch falls back to the last copy on disk.

    `permanent` marks a page about a closed period (a past session): any
    cached copy is final. The caller drops a copy that turns out not to cover
    the period asked for (see fetch_broker_day). `fresh_since` (ISO date)
    accepts any copy fetched on or after it, for pages that change weekly.
    """
    cp = fd._cache_path(source, key)
    cached = None
    if os.path.exists(cp):
        with open(cp, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if (rebuild_only or permanent
                or (cached.get("_fetchedOn") or "") >= (fresh_since or date.today().isoformat())):
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
    # The page's own totals (合計買超張數 / 合計賣超張數); their difference is
    # the day's main-force net, the same figure as the CZCO series.
    text = _cell_text(body)
    tot = {k: re.search(k + r"\s*([\d,]+)", text) for k in ("合計買超張數", "合計賣超張數")}
    return {"updated": fd.parse_twse_date(m.group(1)) if m else None,
            "buy": buy, "sell": sell,
            "totalBuy": fd.parse_num(tot["合計買超張數"].group(1)) if tot["合計買超張數"] else None,
            "totalSell": fd.parse_num(tot["合計賣超張數"].group(1)) if tot["合計賣超張數"] else None}


def fetch_broker_day(code, d, rebuild_only=False):
    """
    One session's broker table, or None. DJ answers a day it has not published
    yet with an empty table or another day's; neither is kept, so the next run
    asks again.
    """
    y, m, dd = (int(x) for x in d.split("-"))
    key = f"{code}_{d.replace('-', '')}"
    b = parse_brokers(fetch_page("DJ_ZCO_DAY", key,
                                 DJ_BROKER_DAY_URL.format(code=code, d=f"{y}-{m}-{dd}"),
                                 "cp950", rebuild_only, permanent=True))
    if b is None or b["updated"] != d:
        cp = fd._cache_path("DJ_ZCO_DAY", key)
        if os.path.exists(cp):
            os.remove(cp)
        return None
    return b


def compact_brokers(b):
    """Per-day tables ride along for every session: rows as arrays, not objects."""
    row = lambda x: [x["name"], x["buy"], x["sell"], x["net"], x["pct"]]  # noqa: E731
    return {"buy": [row(x) for x in b["buy"]], "sell": [row(x) for x in b["sell"]],
            "totalBuy": b.get("totalBuy"), "totalSell": b.get("totalSell")}


# --------------------------------------------------------------------------
# Build one stock
# --------------------------------------------------------------------------

JS_PREFIX = re.compile(r'^\(window\.STOCK_DATA=window\.STOCK_DATA\|\|\{\}\)\["[^"]*"\]=')


def load_previous(code):
    """The last output for `code` (its .js file), or {}."""
    try:
        with open(os.path.join(OUT_DIR, f"{code}.js"), "r", encoding="utf-8") as f:
            text = f.read().strip()
        prev = json.loads(JS_PREFIX.sub("", text).rstrip(";"))
        return prev if isinstance(prev, dict) else {}
    except (OSError, ValueError):
        return {}


def holders_fresh_since(today=None):
    """TDCC data is weekly; a shareholder page fetched since the last Saturday is current."""
    today = today or date.today()
    return (today - timedelta(days=(today.weekday() - 5) % 7)).isoformat()


def build_stock(code, name, days, market, tracked, rebuild_only=False):
    """
    market: {date: (quotes, t86_rows, margin_rows)} shared across stocks.
    `tracked` adds the per-stock broker data (main force, broker tables).
    Returns None for a code with no quotes in range.
    """
    days = [d for d in days if code in market[d][0]]
    if not days:
        return None
    prev = load_previous(code)
    lots = lambda v: round(v / LOTS) if v is not None else None  # noqa: E731

    main, broker_daily, brokers = {}, {}, {}
    if tracked:
        main = {r["date"]: r.get("mainForce") for r in prev.get("daily") or []
                if r.get("mainForce") is not None}
        main.update(parse_main_series(
            fetch_page("DJ_CZCO", code, DJ_SERIES_URL.format(code=code), "cp950", rebuild_only),
            days))
        # Per-day broker tables for every session in range. The first run walks
        # the whole range (one request a day); after that only new days are asked.
        for i, d in enumerate(days, 1):
            b = fetch_broker_day(code, d, rebuild_only)
            if b is not None:
                broker_daily[d] = compact_brokers(b)
                # The CZCO series reaches back ~45 sessions; the tables fill the rest.
                if main.get(d) is None and b["totalBuy"] is not None and b["totalSell"] is not None:
                    main[d] = b["totalBuy"] - b["totalSell"]
            if i % 50 == 0:
                print(f"    {code} broker tables {i}/{len(days)}", flush=True)
        for label, suffix in DJ_BROKER_PERIODS.items():
            b = parse_brokers(fetch_page("DJ_ZCO", f"{code}{suffix or '_1'}",
                                         DJ_BROKER_URL.format(code=code, suffix=suffix),
                                         "cp950", rebuild_only))
            if b is None:
                b = (prev.get("brokers") or {}).get(label)
            if b is not None:
                brokers[label] = b

    daily = []
    for d in days:
        quotes, inst, mg = market[d]
        o, h, lo, c, vol = quotes[code]
        f, t, dl = inst.get(code) or [None, None, None]
        prev_bal, bal = mg.get(code) or [None, None]
        daily.append({
            "date": d, "open": o, "high": h, "low": lo, "close": c, "volume": vol,
            "foreign": lots(f), "trust": lots(t), "dealer": lots(dl),
            "marginBalance": bal,
            "marginChange": bal - prev_bal if bal is not None and prev_bal is not None else None,
            "mainForce": main.get(d),
        })

    holders = parse_holders(
        fetch_page("TWSTHR", code, HOLDERS_URL.format(code=code), "utf-8", rebuild_only,
                   fresh_since=holders_fresh_since()))
    if not holders:
        holders = prev.get("holders") or []

    return {
        "code": code, "name": name or prev.get("name") or code, "tracked": tracked,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "start": days[0], "end": days[-1],
        "daily": daily, "holders": holders, "brokers": brokers,
        "brokerDaily": broker_daily,
    }


def write_stock(s):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"{s['code']}.js"), "w", encoding="utf-8") as f:
        f.write("(window.STOCK_DATA=window.STOCK_DATA||{})[" + json.dumps(s["code"]) + "]="
                + json.dumps(s, ensure_ascii=False, separators=(",", ":")) + ";\n")


def write_index(watchlist):
    """
    The page's stock picker: every stock with an output file, watchlist first
    (in list order, the first being the page's default), then by code.
    """
    built = sorted(f[:-3] for f in os.listdir(OUT_DIR)
                   if f.endswith(".js") and f != "index.js")
    order = [c for c in watchlist if c in built] + [c for c in built if c not in watchlist]
    entries = []
    for code in order:
        s = load_previous(code)
        if s:
            entries.append({"code": code, "name": s.get("name"), "end": s.get("end"),
                            "tracked": code in watchlist})
    with open(os.path.join(OUT_DIR, "index.js"), "w", encoding="utf-8") as f:
        f.write("window.STOCK_LIST=" + json.dumps(entries, ensure_ascii=False,
                                                  separators=(",", ":")) + ";\n")
    return entries


def main():
    ap = argparse.ArgumentParser(description="Fetch single-stock data for stock.html")
    ap.add_argument("--stocks", help="comma-separated codes to build (with broker data)")
    ap.add_argument("--watchlist-only", action="store_true",
                    help="build only the stocks in stocks.txt")
    ap.add_argument("--months", type=int, default=DEFAULT_MONTHS,
                    help=f"months of history (default {DEFAULT_MONTHS})")
    ap.add_argument("--rebuild", action="store_true",
                    help="issue no requests; rebuild outputs from the cache only")
    args = ap.parse_args()

    watchlist = read_watchlist()
    explicit = [c.strip() for c in args.stocks.split(",") if c.strip()] if args.stocks else []
    for c in watchlist + explicit:
        if not re.fullmatch(r"[0-9A-Z]{4,6}", c):
            raise SystemExit(f"ERROR: {c!r} is not a stock code")
    tracked = set(watchlist) | set(explicit)

    today = date.today()
    end = f"{today.year:04d}-{today.month:02d}"
    y, m = today.year, today.month - (args.months - 1)
    while m < 1:
        y, m = y - 1, m + 12
    months = fd.month_range(f"{y:04d}-{m:02d}", end)
    t0 = time.time()
    print(f"[{'REBUILD' if args.rebuild else 'FETCH'}] {months[0]} -> {months[-1]}")

    # The TAIEX months (shared with fetch_data.py's cache) define the calendar.
    calendar = set()
    for ym in months:
        calendar.update(fd.fetch_index_month(ym, args.rebuild))
    calendar = sorted(calendar)
    if not calendar:
        raise SystemExit("ERROR: no trading days retrieved; aborting.")
    print(f"  {len(calendar)} trading days; quotes + institutional + margin, "
          "three whole-market queries per day")

    market, names = {}, {}
    for i, d in enumerate(calendar, 1):
        ymd = d.replace("-", "")
        quotes, nm = fetch_quotes_day(ymd, args.rebuild)
        names.update(nm)                     # later days win: current names
        market[d] = (quotes, fetch_t86_day(ymd, args.rebuild),
                     fetch_margin_all_day(ymd, args.rebuild))
        if i % 25 == 0 or i == len(calendar):
            print(f"  {d}  {i}/{len(calendar)}  {(time.time() - t0) / 60:.1f} min", flush=True)

    # Today's listing decides the universe; a delisted code drops out of it.
    latest = next((market[d][0] for d in reversed(calendar) if market[d][0]), {})
    if explicit:
        codes = explicit
    elif args.watchlist_only:
        codes = watchlist
    else:
        codes = list(dict.fromkeys(
            watchlist + sorted(c for c in latest if UNIVERSE_RE.fullmatch(c))))
    if not codes:
        raise SystemExit("ERROR: no stocks to build. Add codes to stocks.txt.")
    print(f"  building {len(codes)} stock(s), {len(tracked & set(codes))} with broker data")

    built = failed = 0
    for i, code in enumerate(codes, 1):
        try:
            s = build_stock(code, names.get(code), calendar, market, code in tracked,
                            args.rebuild)
        except Exception as e:  # noqa: BLE001 - one bad page must not sink the run
            failed += 1
            print(f"  !! {code} failed: {e}", file=sys.stderr)
            continue
        if s is None:
            if code in tracked:
                print(f"  !! {code}: no TWSE quotes in range (TPEx stocks are not supported)",
                      file=sys.stderr)
            continue
        write_stock(s)
        built += 1
        if s["tracked"]:
            d = s["daily"]
            cov = lambda k: sum(1 for r in d if r[k] is not None)  # noqa: E731
            print(f"  {code} {s['name']}: {len(d)} days ({s['start']} -> {s['end']}) · "
                  f"法人 {cov('foreign')} · 融資 {cov('marginBalance')} · "
                  f"主力 {cov('mainForce')} · 股東週 {len(s['holders'])} · "
                  f"分點 {','.join(s['brokers']) or '-'} · 分點日 {len(s['brokerDaily'])}")
        if i % 100 == 0:
            print(f"  {i}/{len(codes)} stocks  {(time.time() - t0) / 60:.1f} min", flush=True)

    entries = write_index(watchlist)
    st = fd._stats
    print(f"  built {built}, failed {failed} · index: {len(entries)} stock(s) · "
          f"{st['requests']} requests / {st['cache_hits']} cache hits / "
          f"{st['retries']} retries · {(time.time() - t0) / 60:.1f} min")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
