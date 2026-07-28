# -*- coding: utf-8 -*-
"""Hyperscaler GPU pricing from Azure's public retail price API.

The tier Silicon Data charges for (`index_version=hs`). Azure publishes every
VM SKU's price with no authentication and no key, so the hyperscaler layer is
free — AWS's equivalent is a 140 MB regional bundle that needs streaming, and
GCP's Billing Catalog needs a key.

IMPORTANT distinction, because conflating these is the easiest way to draw a
wrong conclusion from this panel:

  on-demand list   what Azure charges a walk-up customer. A real, citable,
                   daily series. Runs 5-6x the neocloud median.
  Low Priority     evictable capacity. Empirically the band that brackets the
                   published hyperscaler *contract* indices.
  Spot             market-clearing evictable price, lands near neocloud levels.

A negotiated enterprise contract sits somewhere between Spot and on-demand and
is not public anywhere. Anyone quoting "the hyperscaler price" without saying
which of these they mean is quoting a number that can differ by 5x.

    python azure.py --out ./data
"""
import argparse, csv, datetime as dt, io, json, os, statistics
import urllib.parse, urllib.request

API = "https://prices.azure.com/api/retail/prices"
UA = "Mozilla/5.0 (compatible; dc-watch/1.0)"
TIMEOUT = 45

# (Azure SKU fragment, our label, GPUs per VM). GPUs per VM is from Azure's own
# SKU documentation; the API does not return a GPU count field.
SKUS = [
    ("ND96isr_H100_v5",  "H100",   8),
    ("ND96amsr_A100_v4", "A100",   8),
    ("ND96asr_v4",       "A100_40G", 8),
    ("ND96isr_MI300X",   "MI300X", 8),
    ("ND96is_H200",      "H200",   8),
    ("NC40ads_H100_v5",  "H100_1x", 1),
]


def fetch(sku):
    f = ("serviceName eq 'Virtual Machines' and priceType eq 'Consumption' "
         "and contains(armSkuName,'%s')" % sku)
    url = API + "?$filter=" + urllib.parse.quote(f)
    out, seen = [], 0
    while url and seen < 5:               # paginate defensively
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            js = json.loads(r.read().decode("utf-8", "replace"))
        out.extend(js.get("Items") or [])
        url = js.get("NextPageLink")
        seen += 1
    return out


def tier(meter):
    m = (meter or "")
    if "Spot" in m:
        return "spot"
    if "Low Priority" in m:
        return "lowpri"
    return "ondemand"


def collect(outdir):
    now = dt.datetime.now()
    rows = []
    for sku, label, ngpu in SKUS:
        try:
            items = fetch(sku)
        except Exception as e:
            print("  ! %s: %s" % (label, str(e)[:80]))
            continue
        if not items:
            print("  - %-9s Azure 无此机型" % label)
            continue
        buckets = {}
        for it in items:
            p = it.get("retailPrice") or 0
            if p <= 0:
                continue
            buckets.setdefault(tier(it.get("meterName")), []).append(p / float(ngpu))
        rec = {"run_date": now.date().isoformat(), "run_time": now.strftime("%H:%M"),
               "gpu": label, "gpus_per_vm": ngpu, "sku": sku,
               "regions": len(items), "source": "azure_retail"}
        for t in ("ondemand", "lowpri", "spot"):
            v = sorted(buckets.get(t) or [])
            rec["%s_median" % t] = round(statistics.median(v), 4) if v else ""
            rec["%s_min" % t] = round(v[0], 4) if v else ""
            rec["%s_n" % t] = len(v)
        rows.append(rec)

    if rows:
        os.makedirs(outdir, exist_ok=True)
        from tracker import append_csv
        append_csv(os.path.join(outdir, "gpu_hyperscaler.csv"),
                   list(rows[0].keys()), rows)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./data")
    a = ap.parse_args()
    rows = collect(a.out)
    print()
    print("  %-9s %10s %10s %10s %8s" % ("GPU", "按需", "LowPri", "Spot", "区域数"))
    for r in rows:
        f = lambda k: ("$%7.2f" % r[k]) if r.get(k) != "" else "      n/a"
        print("  %-9s %10s %10s %10s %8d"
              % (r["gpu"], f("ondemand_median"), f("lowpri_median"),
                 f("spot_median"), r["regions"]))
    print()
    print("  单位 $/GPU-hour（已按每台 GPU 数折算）。按需为挂牌价，非合约价；")
    print("  企业合约价不公开，落在 Spot 与按需之间。")


if __name__ == "__main__":
    main()
