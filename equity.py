# -*- coding: utf-8 -*-
"""The AI-infrastructure equity complex, grouped by position in the chain.

Price and 52-week range only. Consensus estimates and revision breadth are
deliberately absent: those need a paid terminal (Bloomberg / FactSet / LSEG),
and a dashboard that implied it had them would be lying. Short interest is
excluded for a different reason — FINRA publishes it twice a month, so it does
not belong in a daily panel.

What this layer is actually for: the physical indicators upstream (GPU price,
availability, memory) move first; equities reprice against them. Putting them
on one page makes the gap visible.

    python equity.py --out ./data
"""
import argparse, csv, datetime as dt, io, json, os, time, urllib.request

CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
         "?range=1mo&interval=1d")
UA = "Mozilla/5.0 (compatible; dc-watch/1.0)"
TIMEOUT = 30

# Grouped by where the name sits in the AI-infra chain, so the dashboard can
# show which link is repricing rather than one undifferentiated ticker list.
UNIVERSE = [
    ("NVDA", "算力芯片"), ("AMD", "算力芯片"), ("AVGO", "定制ASIC"),
    ("MRVL", "定制ASIC"), ("TSM", "代工"),
    ("MU", "存储"), ("SK hynix", None),          # placeholder, resolved below
    ("ANET", "网络"), ("CRDO", "网络"),
    ("COHR", "光模块"), ("LITE", "光模块"),
    ("VRT", "电源散热"), ("SMCI", "服务器"), ("DELL", "服务器"),
]
UNIVERSE = [(t, g) for t, g in UNIVERSE if g]     # drop unresolved names


def fetch(sym):
    req = urllib.request.Request(CHART % sym, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def parse(sym, group, js):
    res = (js.get("chart") or {}).get("result") or []
    if not res:
        return None
    r = res[0]
    m = r.get("meta") or {}
    closes = [c for c in ((r.get("indicators", {}).get("quote") or [{}])[0]
                          .get("close") or []) if c]
    px = m.get("regularMarketPrice")
    if px is None and closes:
        px = closes[-1]
    if px is None:
        return None
    # meta.chartPreviousClose is the close BEFORE the requested window, not the
    # prior session — using it on a 1mo request produced ~-29% "daily" moves.
    # The prior session is the second-to-last close in the series.
    prev = closes[-2] if len(closes) >= 2 else None
    hi, lo = m.get("fiftyTwoWeekHigh"), m.get("fiftyTwoWeekLow")
    out = {
        "run_date": dt.date.today().isoformat(),
        "ticker": sym,
        "group": group,
        "price": round(float(px), 4),
        "currency": m.get("currency", ""),
        "chg_1d_pct": round(100.0 * (px - prev) / prev, 2) if prev else "",
        "chg_1mo_pct": round(100.0 * (px - closes[0]) / closes[0], 2) if closes else "",
        "wk52_high": hi, "wk52_low": lo,
        # distance below the 52w high is the cleanest single read on how much
        # of the cycle is already priced out of a name
        "pct_below_52w_high": round(100.0 * (hi - px) / hi, 2) if hi else "",
        "pct_above_52w_low": round(100.0 * (px - lo) / lo, 2) if lo else "",
    }
    return out


def collect(outdir, delay=0.6):
    rows = []
    for i, (sym, group) in enumerate(UNIVERSE):
        try:
            r = parse(sym, group, fetch(sym))
            if r:
                rows.append(r)
        except Exception as e:
            print("  ! %s: %s" % (sym, str(e)[:80]))
        if i < len(UNIVERSE) - 1:
            time.sleep(delay)
    if rows:
        os.makedirs(outdir, exist_ok=True)
        from tracker import append_csv
        append_csv(os.path.join(outdir, "equity.csv"), list(rows[0].keys()), rows)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./data")
    a = ap.parse_args()
    rows = collect(a.out)
    print()
    print("  %-7s %-10s %10s %8s %9s %11s"
          % ("ticker", "group", "price", "1d%", "1mo%", "距52周高"))
    for r in sorted(rows, key=lambda x: (x["group"], x["ticker"])):
        print("  %-7s %-10s %10.2f %8s %9s %10s%%"
              % (r["ticker"], r["group"], r["price"], r["chg_1d_pct"],
                 r["chg_1mo_pct"], r["pct_below_52w_high"]))
    print()
    print("  仅价格与 52 周区间。Consensus / Revision 需付费终端，本层不提供；")
    print("  Short interest 为 FINRA 双月度数据，不适合日频面板。")


if __name__ == "__main__":
    main()
