#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SENBA Research 日次終値（JPY）参照モジュール

方針
----
- **参照は完全オフライン**。`rates.json` に収録済みの日次終値だけを使う。
  レポートの数値は後から検証できる必要があるので、生成のたびに外部APIを
  叩いて値が揺れる、という作りにはしない。
- 表の更新は `update` サブコマンドで明示的に行う（Kraken 公開API）。
  claude.ai のサンドボックスは外部通信が遮断されているため、
  **Claude Code など通信可能な環境で実行すること**。
- 通信が使えない環境では `import` サブコマンドで CSV / 貼り付けから取り込む。

収録形式（rates.json）
---------------------
    {
      "_meta": {"updated": "2026-08-15T…", "source": "Kraken OHLC 1440"},
      "BTC": {"2026-08-03": 9843301.0, …},
      "SOL": {…}
    }
"""
import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RATES_PATH = os.path.join(HERE, "rates.json")

# 直接のJPYペアと、無い場合に使う USD ペア（COIN/USD × USD/JPY で合成）
KRAKEN_PAIRS = {
    "BTC":  ("XBTJPY",  "XBTUSD"),
    # ETH/JPY は Kraken では板が薄く、取引が無い日は前日終値が据え置かれる
    # （2026-07-10〜07-14 が5日連続で同値になる等）。円換算の精度を優先し、
    # 流動性のある ETH/USD × USD/JPY で合成する。
    "ETH":  (None,      "ETHUSD"),
    "SOL":  ("SOLJPY",  "SOLUSD"),
    "USDT": ("USDTJPY", "USDTZUSD"),
    "USDC": ("USDCJPY", "USDCUSD"),
    # Kraken に XRP/JPY 板は存在しない（XRPJPY・XXRPZJPY とも Invalid）。
    # ETH と同様に USD 建てから合成する。
    "XRP":  (None,      "XRPUSD"),
    "TRX":  ("TRXJPY",  "TRXUSD"),
    "DOGE": ("DOGEJPY", "XDGUSD"),
    "LTC":  ("LTCJPY",  "LTCUSD"),
    "BCH":  ("BCHJPY",  "BCHUSD"),
    "ADA":  ("ADAJPY",  "ADAUSD"),
    "AVAX": (None,      "AVAXUSD"),
    "BNB":  (None,      "BNBUSD"),
    # USD 建ての被害額をそのまま円換算したい場合に使う（USDT/USDC とは別物）
    "USD":  ("USDJPY",  None),
}

ALIASES = {"XBT": "BTC", "WBTC": "BTC", "WETH": "ETH", "XDG": "DOGE", "USDT.E": "USDT"}


# ---------------------------------------------------------------- 読み書き
def _load():
    with open(RATES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _save(obj):
    tmp = RATES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, RATES_PATH)


_R = _load()


def normalize(coin):
    c = str(coin).strip().upper()
    return ALIASES.get(c, c)


def coins():
    """収録されている通貨コードの一覧（データが1件でもあるもの）"""
    return sorted(k for k, v in _R.items() if not k.startswith("_") and v)


# ------------------------------------------------------- 既存API（互換維持）
def has_coin(coin):
    c = normalize(coin)
    return bool(_R.get(c))


def coverage(coin="ETH"):
    """(最古日, 最新日, 件数)。未収録なら KeyError ではなく None を返す"""
    c = normalize(coin)
    m = _R.get(c)
    if not m:
        return None
    ks = sorted(m)
    return ks[0], ks[-1], len(ks)


def find_rate(coin, iso, max_lookback=5):
    """当日がなければ最大 max_lookback 日前まで遡る。
    戻り値: (rate, used_date, fallback) / 見つからなければ None"""
    c = normalize(coin)
    m = _R.get(c)
    if not m:
        return None
    if iso in m:
        return m[iso], iso, False
    d = datetime.date.fromisoformat(iso)
    for _ in range(max_lookback):
        d -= datetime.timedelta(days=1)
        k = d.isoformat()
        if k in m:
            return m[k], k, True
    return None


# ---------------------------------------------------------------- 変換補助
def jst_to_utc_date(datetime_str):
    """'2026-07-02 07:15:00'（JST）→ UTC基準の日付 '2026-07-01'

    Kraken の日足は UTC 区切りなので、JST 00:00〜08:59 の送金は
    UTC では前日扱いになる。時刻が無ければ日付をそのまま返す。
    """
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", datetime_str)
    if not m:
        raise ValueError("日付を読み取れません: %r" % datetime_str)
    date = "%s-%s-%s" % m.groups()
    t = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", datetime_str)
    if not t:
        return date
    dt = datetime.datetime(
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        int(t.group(1)), int(t.group(2)), int(t.group(3) or 0),
        tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
    return dt.astimezone(datetime.timezone.utc).date().isoformat()


def convert(coin, datetime_str, amount, tz="asis", max_lookback=5):
    """1件を円換算する。

    戻り値 dict: coin / date / rate_date / used_date / fallback / rate / amount / jpy
    レートが引けない場合は RuntimeError（黙って別日を使わない）
    """
    c = normalize(coin)
    date = re.search(r"(\d{4}-\d{2}-\d{2})", datetime_str)
    if not date:
        raise ValueError("日付を読み取れません: %r" % datetime_str)
    date = date.group(1)
    rate_date = jst_to_utc_date(datetime_str) if tz == "jst" else date

    if not has_coin(c):
        raise RuntimeError(
            "%s のレート表がありません。収録済み: %s\n"
            "  → 通信可能な環境で  python3 rates.py update --coins %s\n"
            "  → または            python3 rates.py import <csv> --coin %s"
            % (c, ", ".join(coins()) or "(なし)", c, c))

    f = find_rate(c, rate_date, max_lookback)
    if f is None:
        lo, hi, _n = coverage(c)
        raise RuntimeError(
            "%s %s のレートが見つかりません（収録範囲 %s〜%s）。"
            "rates.py update で表を更新してください。" % (c, rate_date, lo, hi))

    rate, used, fb = f
    amount = float(amount)
    return {"coin": c, "date": date, "rate_date": rate_date, "used_date": used,
            "fallback": fb, "rate": rate, "amount": amount, "jpy": rate * amount}


def fmt_rate(rate):
    """レート表示。USDT/TRX のような低単価通貨は小数を落とすと
    「数量 × レート ≠ 換算額」に見えてしまうため桁を残す。"""
    if rate >= 1000:
        return format(int(round(rate)), ",")
    if rate >= 1:
        return format(rate, ",.2f")
    return format(rate, ",.6f").rstrip("0").rstrip(".")


def footnote(rows, tz="asis"):
    """報告書貼付用の脚注を組み立てる"""
    if not rows:
        return ""
    used = sorted({r["coin"] for r in rows})
    dates = sorted(r["rate_date"] for r in rows)
    s = ("円換算額は参考値であり、%s について Kraken（海外暗号資産取引所）における"
         "該当日のJPY建て日足終値レート（UTC基準）を使用して算出している。"
         "対象期間は%sから%sまで。" % ("・".join(used), dates[0], dates[-1]))
    if tz == "jst":
        s += "なお、取引日時は日本標準時（JST）表記として扱い、対応するUTC日の終値を採用している。"
    if any(r["fallback"] for r in rows):
        s += ("該当日のレートが存在しない日については、直近の取引日の終値を代用しており、"
              "当該行に注記を付している。")
    return s


# ---------------------------------------------------------------- 取り込み
_DATE_RE = re.compile(r"(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})日?")


def _split_fields(line):
    """桁区切りカンマと CSV 区切りカンマを取り違えないよう列に分解する"""
    if '"' in line:
        out = []
        for m in re.finditer(r'"([^"]*)"|([^,\t]+)', line):
            v = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
            if v:
                out.append(v)
        return out
    if "\t" in line:
        return [s.strip() for s in line.split("\t") if s.strip()]
    f = [s.strip() for s in line.split(",") if s.strip()]
    if len(f) < 2:
        f = [s.strip() for s in line.split() if s.strip()]
    return f


def parse_rate_text(text):
    """「日付 + 終値」のテキスト/CSV を {ISO日付: 終値} に変換"""
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = _split_fields(line)
        di = next((i for i, f in enumerate(fields) if _DATE_RE.search(f)), None)
        if di is None:
            continue
        g = _DATE_RE.search(fields[di]).groups()
        iso = "%s-%02d-%02d" % (g[0], int(g[1]), int(g[2]))
        for f in fields[di + 1:]:
            if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", f):
                v = float(f.replace(",", ""))
                if v > 0:
                    out[iso] = v
                break
    return out


def merge(coin, table, meta_source=None):
    """{日付: 終値} を rates.json に統合して保存。戻り値 (新規件数, 上書き件数)"""
    c = normalize(coin)
    cur = _R.setdefault(c, {})
    new = sum(1 for k in table if k not in cur)
    upd = sum(1 for k, v in table.items() if k in cur and cur[k] != v)
    cur.update(table)
    meta = _R.setdefault("_meta", {})
    meta["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if meta_source:
        meta.setdefault("source", {})[c] = meta_source
    _save(_R)
    return new, upd


# ---------------------------------------------------------- Kraken 取得
def _kraken_ohlc(pair, timeout=20):
    """日足終値 {日付: close}。未確定の当日足は除外。
    ペアが存在しない場合は None を返す。"""
    import urllib.request
    import urllib.error
    url = "https://api.kraken.com/0/public/OHLC?pair=%s&interval=1440" % pair
    req = urllib.request.Request(url, headers={"User-Agent": "senba-report/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        payload = json.loads(res.read().decode("utf-8"))
    err = payload.get("error") or []
    if err:
        if any(("Unknown asset pair" in e) or ("Invalid asset pair" in e)
               for e in err):
            return None
        raise RuntimeError("Kraken エラー: %s" % " / ".join(err))
    result = payload.get("result", {})
    key = next((k for k in result if k != "last"), None)
    if key is None:
        raise RuntimeError("Kraken レスポンス形式が想定外です")
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    out = {}
    for row in result[key]:
        iso = datetime.datetime.fromtimestamp(
            row[0], datetime.timezone.utc).date().isoformat()
        if iso >= today:          # 未確定の当日足は採用しない
            continue
        close = float(row[4])
        if close > 0:
            out[iso] = close
    return out


def update(target_coins, verbose=True):
    """Kraken から日足終値を取り込む。要ネットワーク。"""
    import time
    import urllib.error
    log = []
    usdjpy = None

    for coin in target_coins:
        c = normalize(coin)
        if c not in KRAKEN_PAIRS:
            log.append("%-5s スキップ（ペア未定義。--pair で指定するか import を使用）" % c)
            continue
        jpy_pair, usd_pair = KRAKEN_PAIRS[c]
        table, source = None, None
        try:
            if jpy_pair:
                table = _kraken_ohlc(jpy_pair)
                source = "Kraken %s (1440)" % jpy_pair
            if table is None and usd_pair:
                if usdjpy is None:
                    usdjpy = _kraken_ohlc("USDJPY") or {}
                usd = _kraken_ohlc(usd_pair)
                if usd:
                    table = {d: v * usdjpy[d] for d, v in usd.items() if d in usdjpy}
                    source = "Kraken %s × USDJPY (1440)" % usd_pair
        except urllib.error.URLError as e:
            log.append("%-5s 取得失敗（ネットワーク到達不可: %s）" % (c, e.reason))
            continue
        except Exception as e:  # noqa: BLE001
            log.append("%-5s 取得失敗（%s）" % (c, e))
            continue

        if not table:
            log.append("%-5s Kraken に該当ペアなし" % c)
            continue
        new, upd = merge(c, table, source)
        ks = sorted(table)
        log.append("%-5s %4d件 %s〜%s（新規 %d / 更新 %d） %s"
                   % (c, len(table), ks[0], ks[-1], new, upd, source))
        time.sleep(0.4)   # Kraken のレート制限に配慮

    if verbose:
        print("\n".join(log))
    return log


# ---------------------------------------------------------------- CLI
def _cmd_coverage(a):
    rows = []
    for c in sorted(k for k in _R if not k.startswith("_")):
        cv = coverage(c)
        rows.append((c, "0", "-", "-") if cv is None
                    else (c, str(cv[2]), cv[0], cv[1]))
    w = max(len(r[0]) for r in rows) if rows else 4
    print("%-*s %6s  %-10s  %-10s" % (w, "COIN", "件数", "最古", "最新"))
    today = datetime.date.today()
    for c, n, lo, hi in rows:
        lag = ""
        if hi != "-":
            days = (today - datetime.date.fromisoformat(hi)).days
            if days > 2:
                lag = "  ← %d日前まで" % days
        print("%-*s %6s  %-10s  %-10s%s" % (w, c, n, lo, hi, lag))
    meta = _R.get("_meta", {})
    if meta.get("updated"):
        print("\n最終更新: %s" % meta["updated"])


def _cmd_lookup(a):
    r = convert(a.coin, a.date, 1, tz=a.tz)
    print("%s %s  終値 %s円%s"
          % (r["coin"], r["rate_date"], fmt_rate(r["rate"]),
             "  ※%s の終値を代用" % r["used_date"] if r["fallback"] else ""))


def _cmd_convert(a):
    r = convert(a.coin, a.datetime, a.amount, tz=a.tz)
    print("%s %s × %s = %s円%s"
          % (r["coin"], r["amount"], fmt_rate(r["rate"]),
             format(round(r["jpy"]), ","),
             "  ※%s の終値を代用" % r["used_date"] if r["fallback"] else ""))


def _cmd_batch(a):
    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    rows, errors = [], []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        f = _split_fields(line)
        coin = next((x.upper() for x in f
                     if re.fullmatch(r"[A-Za-z]{2,6}", x)
                     and normalize(x) in KRAKEN_PAIRS), None) or a.coin
        if coin is None:
            errors.append("%d行目: 通貨が判別できません" % i)
            continue
        amt = None
        for x in reversed(f):
            if ":" in x or _DATE_RE.search(x):
                continue
            try:
                v = float(x.replace(",", ""))
            except ValueError:
                continue
            if v > 0:
                amt = v
                break
        if amt is None:
            errors.append("%d行目: 数量を読み取れません" % i)
            continue
        try:
            rows.append(convert(coin, line, amt, tz=a.tz))
        except (RuntimeError, ValueError) as e:
            errors.append("%d行目: %s" % (i, e))

    print("日付\t通貨\t数量\t終値(JPY)\t円換算額\t備考")
    for r in rows:
        note = "%s の終値を代用" % r["used_date"] if r["fallback"] else ""
        if r["rate_date"] != r["date"]:
            note = ("UTC %s の終値を使用" % r["rate_date"]) + ("／" + note if note else "")
        print("%s\t%s\t%s\t%s\t%d\t%s"
              % (r["date"], r["coin"], r["amount"], fmt_rate(r["rate"]),
                 round(r["jpy"]), note))
    if rows:
        by = {}
        for r in rows:
            by[r["coin"]] = by.get(r["coin"], 0.0) + r["jpy"]
        if len(by) > 1:
            for c, v in sorted(by.items()):
                print("小計 %s\t\t\t\t%d\t" % (c, round(v)))
        print("合計\t\t\t\t%d\t" % round(sum(r["jpy"] for r in rows)))
        print("\n[脚注] " + footnote(rows, tz=a.tz))
    if errors:
        print("\n[エラー %d件]" % len(errors), file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        sys.exit(1)


def _cmd_update(a):
    target = [c.strip().upper() for c in a.coins.split(",")] if a.coins \
        else ["BTC", "ETH", "SOL", "USDT"]
    log = update(target)
    if all("失敗" in l or "なし" in l or "スキップ" in l for l in log):
        print("\n※ 1件も取得できませんでした。claude.ai のサンドボックスは外部通信が"
              "遮断されています（api.kraken.com は host_not_allowed）。\n"
              "  Claude Code など通信可能な環境で実行するか、"
              "`rates.py import` で手元のCSVを取り込んでください。", file=sys.stderr)
        sys.exit(1)


def _cmd_import(a):
    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    table = parse_rate_text(text)
    if not table:
        sys.exit("[中断] 認識できる「日付 + 終値」がありませんでした。")
    new, upd = merge(a.coin, table, a.source or "手動取り込み")
    ks = sorted(table)
    print("%s に %d件取り込みました（%s〜%s／新規 %d・更新 %d）"
          % (normalize(a.coin), len(table), ks[0], ks[-1], new, upd))


def main():
    p = argparse.ArgumentParser(
        description="SENBA Research 日次終値（JPY）参照ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""例:
  python3 rates.py coverage
  python3 rates.py lookup SOL 2026-07-02
  python3 rates.py convert --coin USDT --datetime "2026-07-05 20:00:01" --amount 48000
  python3 rates.py batch outflows.tsv --tz jst
  python3 rates.py update --coins BTC,ETH,SOL,USDT      # 要ネットワーク
  python3 rates.py import sol_jpy.csv --coin SOL
""")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("coverage", help="収録状況を表示")
    s.set_defaults(func=_cmd_coverage)

    s = sub.add_parser("lookup", help="ある日の終値を引く")
    s.add_argument("coin")
    s.add_argument("date")
    s.add_argument("--tz", choices=["asis", "jst"], default="asis")
    s.set_defaults(func=_cmd_lookup)

    s = sub.add_parser("convert", help="1件を円換算")
    s.add_argument("--coin", required=True)
    s.add_argument("--datetime", required=True)
    s.add_argument("--amount", required=True, type=float)
    s.add_argument("--tz", choices=["asis", "jst"], default="asis")
    s.set_defaults(func=_cmd_convert)

    s = sub.add_parser("batch", help="「日時 数量 [通貨]」の一覧をまとめて換算")
    s.add_argument("file", help="ファイルパス、または - で標準入力")
    s.add_argument("--coin", help="行に通貨表記が無いときの既定")
    s.add_argument("--tz", choices=["asis", "jst"], default="asis")
    s.set_defaults(func=_cmd_batch)

    s = sub.add_parser("update", help="Kraken から日足終値を取り込む（要ネットワーク）")
    s.add_argument("--coins", help="カンマ区切り。既定 BTC,ETH,SOL,USDT")
    s.set_defaults(func=_cmd_update)

    s = sub.add_parser("import", help="CSV/貼り付けから「日付 + 終値」を取り込む")
    s.add_argument("file", help="ファイルパス、または - で標準入力")
    s.add_argument("--coin", required=True)
    s.add_argument("--source", help="出典メモ（例: investing.com SOL/JPY）")
    s.set_defaults(func=_cmd_import)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
