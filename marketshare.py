# -*- coding: utf-8 -*-
"""Vendor-level weekly token share, with a year of history already in it.

The model-level rankings endpoint only exposes a rolling window, so our own
vendor-share series starts the day we begin collecting. This endpoint returns
52 completed weeks in one call, which is the single cheapest way to get real
year-over-year structure into the dashboard.

Found by way of the Tianfeng research workbook, which cites the path without
the /v1/ segment — that one now 404s, the same migration the model rankings
endpoint went through. Always probe before trusting a cited path.

    python marketshare.py --out ./data
"""
import argparse, csv, datetime as dt, io, json, os, urllib.request

API = "https://openrouter.ai/api/frontend/v1/rankings/market-share"
UA = "Mozilla/5.0 (compatible; dc-watch/1.0)"
TIMEOUT = 45

# `others` is the endpoint's own residual bucket, not a vendor.
RESIDUAL = "others"
US_CORE = {"openai", "anthropic", "google", "x-ai", "meta-llama",
           "microsoft", "nvidia", "amazon"}


def fetch():
    req = urllib.request.Request(API, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))["data"]


def collect(outdir):
    weeks = fetch()
    if not weeks:
        print("  ! market-share returned no weeks")
        return []
    today = dt.date.today().isoformat()
    rows = []
    for w in weeks:
        wk = str(w.get("x") or "")[:10]
        ys = w.get("ys") or {}
        total = sum(ys.values()) or 1
        for author, tok in ys.items():
            rows.append({
                "run_date": today,
                "week": wk,
                "author": author,
                "tokens": int(tok),
                "share_pct": round(100.0 * tok / total, 3),
                "is_residual": 1 if author == RESIDUAL else 0,
                "is_us": 1 if author in US_CORE else 0,
            })
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "market_share.csv")
    # a full replace, not an append: every call returns the same 52-week window
    # recomputed upstream, so appending would just pile up duplicates
    cols = list(rows[0].keys())
    with io.open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print("  wrote %d row(s) (%d weeks x %d vendors) -> %s"
          % (len(rows), len(weeks), len({r["author"] for r in rows}), path))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./data")
    a = ap.parse_args()
    rows = collect(a.out)
    if not rows:
        return
    weeks = sorted({r["week"] for r in rows})
    first, last = weeks[0], weeks[-1]
    cur = {r["author"]: r for r in rows if r["week"] == last}
    old = {r["author"]: r for r in rows if r["week"] == first}
    print()
    print("  %s .. %s（%d 周）" % (first, last, len(weeks)))
    print("  %-14s %10s %9s %9s" % ("厂商", "本周份额", "一年前", "变化"))
    for a_, r in sorted(cur.items(), key=lambda kv: -kv[1]["share_pct"])[:12]:
        o = (old.get(a_) or {}).get("share_pct", 0.0)
        print("  %-14s %9.1f%% %8.1f%% %+8.1fpct" % (a_, r["share_pct"], o, r["share_pct"] - o))


if __name__ == "__main__":
    main()
