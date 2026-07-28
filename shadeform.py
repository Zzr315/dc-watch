# -*- coding: utf-8 -*-
"""GPU rental price and supply tightness from Shadeform's public aggregator.

A free stand-in for the Silicon Data GPU Index that J.P. Morgan uses. Shadeform
aggregates ~270 instance types across ~18 neoclouds — the same non-hyperscaler
population Silicon Data's `index_version=neo` covers — with no authentication.

Two series come out of it:
  price        median $/GPU-hour per GPU type
  availability share of regions currently marked available, which Silicon Data
               does not publish at all and which leads price at turning points

Validation: `hourly_price` is in CENTS for the whole instance. Cross-checked
against Lambda Labs' published list prices (A100 $1.99, H100 $3.99-4.36 per
GPU-hour) — the dollars reading would give $199/GPU-hour, which is absurd.

    python shadeform.py --out ./data
"""
import argparse, csv, datetime as dt, io, json, os, statistics, urllib.request

API = "https://api.shadeform.ai/v1/instances/types"
UA = "Mozilla/5.0 (compatible; dc-watch/1.0)"
TIMEOUT = 45

# Tracked headline types. JPM covers A100/H100/B200; H200 and B300 are the
# newer parts worth watching, and L40S is the cheap-inference tier.
TRACKED = ["A100", "A100_80G", "H100", "H100_nvl", "H200", "B200", "B300",
           "L40S", "A6000", "RTX4090"]


def fetch():
    req = urllib.request.Request(API, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))["instance_types"]


def summarize(rows):
    """Collapse instance types into one record per GPU type."""
    price, avail, seen_clouds = {}, {}, {}
    for r in rows:
        g = r.get("gpu_type")
        n = r.get("num_gpus") or 0
        p = r.get("hourly_price")
        if not g or not n or not p:
            continue
        per_gpu = (p / 100.0) / n          # cents for the whole box -> $/GPU-hr
        price.setdefault(g, []).append(per_gpu)
        seen_clouds.setdefault(g, set()).add(r.get("cloud"))
        a = avail.setdefault(g, [0, 0])
        for reg in (r.get("availability") or []):
            a[1] += 1
            if reg.get("available"):
                a[0] += 1

    out = []
    now = dt.datetime.now()
    for g in sorted(price):
        v = sorted(price[g])
        got, tot = avail.get(g, [0, 0])
        out.append({
            "run_date": now.date().isoformat(),
            "run_time": now.strftime("%H:%M"),
            "gpu": g,
            "clouds": len(seen_clouds.get(g, ())),
            "offers": len(v),
            "usd_per_gpu_hour_min": round(v[0], 4),
            "usd_per_gpu_hour_median": round(statistics.median(v), 4),
            "usd_per_gpu_hour_max": round(v[-1], 4),
            "regions_total": tot,
            "regions_available": got,
            "availability_pct": round(100.0 * got / tot, 1) if tot else "",
            "source": "shadeform",
        })
    return out


def append_csv(path, rows):
    """Delegates to tracker's schema-reconciling writer so adding a column
    later rewrites the header instead of producing ragged rows."""
    if not rows:
        return
    from tracker import append_csv as _a
    _a(path, list(rows[0].keys()), rows)


def collect(outdir):
    rows = summarize(fetch())
    tracked = [r for r in rows if r["gpu"] in TRACKED]
    os.makedirs(outdir, exist_ok=True)
    append_csv(os.path.join(outdir, "gpu_shadeform.csv"), tracked)
    return tracked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./data")
    a = ap.parse_args()
    rows = collect(a.out)
    print()
    print("  %-10s %7s %7s %7s  %-7s %s" %
          ("gpu", "min", "median", "max", "avail", "clouds"))
    for r in rows:
        print("  %-10s %7.2f %7.2f %7.2f  %5s%%  %d clouds / %d offers"
              % (r["gpu"], r["usd_per_gpu_hour_min"], r["usd_per_gpu_hour_median"],
                 r["usd_per_gpu_hour_max"], r["availability_pct"],
                 r["clouds"], r["offers"]))


if __name__ == "__main__":
    main()
