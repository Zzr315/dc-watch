# -*- coding: utf-8 -*-
"""Serving performance per model: throughput, latency and effective price.

Pulls OpenRouter's per-provider endpoint stats for the highest-volume models.
This is the missing input that turns a token tracker into a unit-economics
tracker — without tokens/second you can price a token but you cannot say what
it costs to serve one.

Throughput is request-weighted across providers rather than a plain median:
each provider reports request_count for the same 30-minute window, and a
provider serving 30k requests should not carry the same weight as one serving
20. Latency is reported in milliseconds by the API and converted to seconds.

Undocumented endpoint. Fails soft: a model that errors is skipped, not fatal.

    python perf.py --data ./data --top 20
"""
import argparse, collections, csv, datetime as dt, glob, io, json, os, statistics, time
import urllib.parse, urllib.request

STATS = "https://openrouter.ai/api/frontend/v1/stats/endpoint?permaslug=%s&variant=%s"
UA = "Mozilla/5.0 (compatible; dc-watch/1.0)"
TIMEOUT = 40


def read_csv(p):
    if not os.path.exists(p):
        return []
    with io.open(p, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def top_models(datadir, window="week", top=20):
    rows = []
    for p in sorted(glob.glob(os.path.join(datadir, "llm_by_model_*.csv"))):
        rows.extend([r for r in read_csv(p) if r.get("window") == window])
    if not rows:
        return []
    latest = max(r["run_date"] for r in rows)
    rows = [r for r in rows if r["run_date"] == latest and r.get("variant") != "free"]
    rows.sort(key=lambda r: -(num(r.get("total_tokens")) or 0))
    seen, out = set(), []
    for r in rows:
        k = (r["model"], r.get("variant") or "standard")
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
        if len(out) >= top:
            break
    return out


def fetch_stats(permaslug, variant):
    url = STATS % (urllib.parse.quote(permaslug, safe="/"),
                   urllib.parse.quote(variant))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace")).get("data") or []


def summarize(permaslug, variant, endpoints):
    tp, lat, price, reqs, wsum, wtp = [], [], [], 0, 0.0, 0.0
    quant = []
    hf = ""
    for e in endpoints:
        st = e.get("stats") or {}
        t = st.get("p50_throughput")
        l = st.get("p50_latency")
        n = st.get("request_count") or 0
        c = num((e.get("pricing") or {}).get("completion"))
        # Serving precision, not release precision. A model published in BF16 is
        # commonly served quantised to FP8 or FP4, which changes its VRAM
        # footprint by 2-4x and therefore the GPUs needed per replica.
        qz = (e.get("quantization") or "").lower()
        if qz and qz not in ("unknown", "none"):
            quant.append(qz)
        hf = hf or ((e.get("model") or {}).get("hf_slug") or "")
        if t:
            tp.append(float(t))
            if n:
                wsum += n
                wtp += float(t) * n
        if l:
            lat.append(float(l) / 1000.0)      # ms -> s
        if c:
            price.append(c * 1e6)              # $ per Mn output tokens
        reqs += n
    if not tp:
        return None
    return {
        "run_date": dt.date.today().isoformat(),
        "model": permaslug,
        "variant": variant,
        "providers": len(endpoints),
        "providers_with_stats": len(tp),
        "tok_s_weighted": round(wtp / wsum, 2) if wsum else round(statistics.median(tp), 2),
        "tok_s_median": round(statistics.median(tp), 2),
        "tok_s_max": round(max(tp), 2),
        "latency_s_median": round(statistics.median(lat), 2) if lat else "",
        "out_price_usd_per_mn_median": round(statistics.median(price), 4) if price else "",
        "out_price_usd_per_mn_min": round(min(price), 4) if price else "",
        "requests_30m": int(reqs),
        # most common serving precision across providers
        "quantization": (collections.Counter(quant).most_common(1)[0][0]
                         if quant else "unknown"),
        "quant_providers": len(quant),
        "hf_slug": hf,
    }


def collect(datadir, top=20, delay=1.2, window="week"):
    models = top_models(datadir, window, top)
    if not models:
        print("  no model list yet - run tracker.py first")
        return []
    out = []
    for i, (slug, variant) in enumerate(models):
        try:
            eps = fetch_stats(slug, variant)
            s = summarize(slug, variant, eps)
            if s:
                out.append(s)
        except Exception as e:
            print("  ! %s: %s" % (slug, str(e)[:80]))
        if i < len(models) - 1:
            time.sleep(delay)          # undocumented endpoint; stay polite
    if out:
        from tracker import append_csv        # schema-reconciling writer
        append_csv(os.path.join(datadir, "model_perf.csv"),
                   list(out[0].keys()), out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./data")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--delay", type=float, default=1.2)
    a = ap.parse_args()
    rows = collect(a.data, a.top, a.delay)
    print()
    print("  %-40s %8s %8s %9s %7s" % ("model", "tok/s", "lat_s", "$out/Mn", "prov"))
    for r in sorted(rows, key=lambda x: -x["tok_s_weighted"]):
        print("  %-40s %8.1f %8s %9s %7d"
              % (r["model"].split("/")[-1][:40], r["tok_s_weighted"],
                 r["latency_s_median"], r["out_price_usd_per_mn_median"],
                 r["providers_with_stats"]))


if __name__ == "__main__":
    main()
