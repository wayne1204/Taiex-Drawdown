#!/usr/bin/env python3
"""
Unit tests for fetch_stocks.py parsers. Fully offline: every input is a small
inline excerpt shaped like the real response, and SESSION.get is patched to
fail loudly if anything tries the network.

Run with:  python3 tests/stock_fetch_test.py
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import fetch_data  # noqa: E402
import fetch_stocks as fs  # noqa: E402


class NetworkAccessAttempted(BaseException):
    """BaseException so fetch()'s `except Exception` retry loop cannot swallow it."""


def _no_network(*_a, **_k):
    raise NetworkAccessAttempted("a test tried to hit the network")


class Base(unittest.TestCase):
    def setUp(self):
        p = patch.object(fetch_data.SESSION, "get", side_effect=_no_network)
        p.start()
        self.addCleanup(p.stop)


class TestT86(Base):
    FIELDS = ["證券代號", "證券名稱", "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
              "外陸資買賣超股數(不含外資自營商)", "外資自營商買進股數", "外資自營商賣出股數",
              "外資自營商買賣超股數", "投信買進股數", "投信賣出股數", "投信買賣超股數",
              "自營商買賣超股數", "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)",
              "自營商買賣超股數(自行買賣)", "自營商買進股數(避險)", "自營商賣出股數(避險)",
              "自營商買賣超股數(避險)", "三大法人買賣超股數"]
    ROW = ["2327", "國巨*  ", "10,486,351", "14,203,233", "-3,716,882", "0", "0", "1,000",
           "1,348,593", "197,000", "1,151,593", "-1,544,564", "44,295", "1,150,392",
           "-1,106,097", "226,004", "664,471", "-438,467", "-4,109,853"]

    def test_columns_by_name(self):
        rows = fs.slim_t86({"fields": self.FIELDS, "data": [self.ROW]})["rows"]
        # foreign = 外陸資 + 外資自營商; dealer = the total column, not a sub-desk
        self.assertEqual(rows["2327"], [-3715882, 1151593, -1544564])

    def test_dirty_value_is_none_not_zero(self):
        row = list(self.ROW)
        row[10] = "--"
        self.assertIsNone(fs.slim_t86({"fields": self.FIELDS, "data": [row]})["rows"]["2327"][1])


class TestMargin(Base):
    def test_margin_rows(self):
        payload = {"tables": [
            {"fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
             "data": [["融資(交易單位)", "1", "2", "3", "4", "5"]]},
            {"fields": ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額",
                        "次一營業日限額", "買進", "賣出", "現券償還", "前日餘額", "今日餘額"],
             "data": [["2327", "國巨*", "1,142", "1,257", "11", "51,866", "51,740",
                       "514,682", "57", "26", "0", "391", "360"]]},
        ]}
        # The first 前日餘額/今日餘額 pair is 融資, not 融券
        self.assertEqual(fs.slim_margin(payload)["rows"], {"2327": [51866, 51740]})


class TestQuotes(Base):
    def test_quote_table_found_by_header(self):
        payload = {"tables": [
            {"fields": ["指數", "收盤指數"], "data": [["發行量加權股價指數", "40,000"]]},
            {"fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價",
                        "最高價", "最低價", "收盤價", "漲跌(+/-)"],
             "data": [["2330", "台積電", "43,046,982", "1", "1", "2,430.00", "2,445.00",
                       "2,420.00", "2,420.00", "<p>-</p>"],
                      ["9999", "停牌股", "0", "0", "0", "--", "--", "--", "--", ""]]},
        ]}
        out = fs.slim_quotes(payload)
        self.assertEqual(out["rows"]["2330"], [2430, 2445, 2420, 2420, 43047])
        self.assertEqual(out["names"]["2330"], "台積電")
        # No trade: prices are gaps, not zeros
        self.assertEqual(out["rows"]["9999"][:4], [None, None, None, None])

    def test_universe(self):
        ok = ["2330", "0050", "00878", "00632R"]
        no = ["2881A", "01001T", "020000", "910322", "1101B"]
        self.assertTrue(all(fs.UNIVERSE_RE.fullmatch(c) for c in ok))
        self.assertFalse(any(fs.UNIVERSE_RE.fullmatch(c) for c in no))


class TestHoldersFreshness(Base):
    def test_weekly_after_saturday(self):
        from datetime import date
        f = lambda d: fs.holders_fresh_since(date.fromisoformat(d))  # noqa: E731
        self.assertEqual(f("2026-10-17"), "2026-10-17")   # Saturday itself
        self.assertEqual(f("2026-10-19"), "2026-10-17")   # Monday
        self.assertEqual(f("2026-10-23"), "2026-10-17")   # Friday
        self.assertEqual(f("2026-10-24"), "2026-10-24")


class TestHolders(Base):
    HEAD = ("<table id='Details'><tr class='lDS'><td>&nbsp;</td><td>&nbsp;</td><td>資料日期</td>"
            "<td>集保</br>總張數</td><td>總股東<br/>人數</td><td>平均</br>張數/人</td>"
            "<td>>400張</br>大股東<br/>持有張數</td><td>>400張</br>大股東<br/>持有百分比</td>"
            "<td>>400張</br>大股東<br/>人數</td><td>400</br>~600張</br>人數</td>"
            "<td>600</br>~800張</br>人數</td><td>800</br>~1000張</br>人數</td>"
            "<td>>1000張</br>人數</td><td>>1000張</br>大股東<br/>持有百分比</td><td>收盤價</td>"
            "<td>&nbsp;</td></tr>")

    @staticmethod
    def row(d, holders):
        cells = ["&nbsp;", "<div>&nbsp;</div>", f"{d}&nbsp;", "2,058,731&nbsp;", holders, "3.32",
                 "1,373,079", "66.70", "358", "87", "47", "28", "196", "61.74", "567.00", "&nbsp;"]
        return "<tr class='lDS'>" + "".join(f"<td>{c}</td>" for c in cells) + "<tr/>"

    def test_parse_sorted_and_split_week_dropped(self):
        body = (self.HEAD + self.row("20261016", "619,908") + self.row("20250822", "1")
                + self.row("20261008", "585,575") + "</table>")
        out = fs.parse_holders(body)
        self.assertEqual([r["date"] for r in out], ["2026-10-08", "2026-10-16"])
        self.assertEqual(out[-1], {"date": "2026-10-16", "holders": 619908, "avgLots": 3.32,
                                   "big400Pct": 66.70, "big400": 358, "big1000": 196,
                                   "big1000Pct": 61.74})


class TestMainSeries(Base):
    def test_year_inferred_from_trading_days(self):
        days = ["2025-12-30", "2025-12-31", "2026-01-02", "2026-01-05"]
        body = "1230,1231,0102,0105 600,610,620,630 -100,200,--,-5"
        self.assertEqual(fs.parse_main_series(body, days),
                         {"2025-12-30": -100, "2025-12-31": 200, "2026-01-05": -5})

    def test_unmatched_latest_point_dropped(self):
        # DJ has a session TWSE's K-bar data does not yet: drop it, keep the rest aligned
        days = ["2026-10-15"]
        self.assertEqual(fs.parse_main_series("1015,1016 1,2 7,8", days), {"2026-10-15": 7})

    def test_garbage(self):
        self.assertEqual(fs.parse_main_series("", ["2026-10-15"]), {})
        self.assertEqual(fs.parse_main_series("1015 1", ["2026-10-15"]), {})


class TestBrokers(Base):
    def test_parse(self):
        body = ("<div class=\"t11\">單位：張　最後更新日：2026/10/16</div>"
                "<TR><TD class=\"t2\">買超券商</TD><TD>買進</TD><TD>賣出</TD><TD>買超</TD><TD>佔成交比重</TD>"
                "<TD>賣超券商</TD><TD>買進</TD><TD>賣出</TD><TD>賣超</TD><TD>佔成交比重</TD></TR>"
                "<TR><TD class=\"t4t1\" nowrap><a href=\"x\">新加坡商瑞銀</a></TD><TD>2,129</TD>"
                "<TD>855</TD><TD>1,274</TD><TD>3.51%</TD><TD><a href=\"y\">美林</a></TD>"
                "<TD>566</TD><TD>3,235</TD><TD>2,669</TD><TD>7.36%</TD></tr>")
        b = fs.parse_brokers(body)
        self.assertEqual(b["updated"], "2026-10-16")
        self.assertEqual(b["buy"], [{"name": "新加坡商瑞銀", "buy": 2129, "sell": 855,
                                     "net": 1274, "pct": 3.51}])
        self.assertEqual(b["sell"][0]["name"], "美林")

    def test_page_totals(self):
        body = ("<div>最後更新日：2026/10/08</div><TR><TD><a>x</a></TD><TD>1</TD><TD>0</TD><TD>1</TD>"
                "<TD>1%</TD><TD><a>y</a></TD><TD>0</TD><TD>1</TD><TD>1</TD><TD>1%</TD></TR>"
                "<tr><td>合計買超張數</td><td>3,804</td><td>合計賣超張數</td><td>13,043</td></tr>")
        b = fs.parse_brokers(body)
        self.assertEqual((b["totalBuy"], b["totalSell"]), (3804, 13043))

    def test_empty_page(self):
        self.assertIsNone(fs.parse_brokers("<html>無資料</html>"))


class TestWatchlist(Base):
    def test_comments_and_dupes(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("# list\n2327  # 國巨\n\n2330\n2327\n")
        self.addCleanup(os.remove, f.name)
        self.assertEqual(fs.read_watchlist(f.name), ["2327", "2330"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
