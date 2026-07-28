# -*- coding: utf-8 -*-
"""One-time: pull historical token VOLUME back to 2025-01-01.

Only volume can be reconstructed. OpenRouter exposes no historical price API,
so spend, VWAP and the price measures start accumulating the day you begin
running the tracker — those columns are deliberately left blank on backfilled
rows rather than back-computed from today's price list, which would be wrong.

Uses the documented dataset endpoint (top 50 per day plus an aggregated
`other` row, so daily totals are complete). Needs OPENROUTER_API_KEY; the
endpoint is rate-limited to 30 requests/minute and 500/day.

    python backfill.py --start 2025-01-01
    python backfill.py --probe-only      # just verify the endpoint and key
"""
import argparse, csv, datetime as dt, io, json, os, sys, time, urllib.error, urllib.request

ENDPOINT = "https://openrouter.ai/api/v1/datasets/rankings-daily"
UA = "Mozilla/5.0 (compatible; dc-watch-backfill/1.0)"
FLOOR = dt.date(2025, 1, 1)
US_CORE = {"openai", "anthropic", "google", "x-ai", "meta-llama",
           "microsoft", "nvidia", "amazon"}


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if v and not os.environ.get(k):
                    os.environ[k] = v


def fetch(key, start, end):
    url = "%s?start_date=%s&end_date=%s&period=day" % (ENDPOINT, start, end)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def month_spans(start, end):
    cur = start
    while cur <= end:
        if cur.month == 12:
            nxt = dt.date(cur.year + 1, 1, 1)
        else:
            nxt = dt.date(cur.year, cur.month + 1, 1)
        yield cur, min(nxt - dt.timedelta(days=1), end)
        cur = nxt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--data", default="./data")
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--sleep", type=float, default=2.5,
                    help="seconds between requests (limit is 30/min)")
    a = ap.parse_args()
    load_env()

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY not set.\n"
              "  Create a free key at https://openrouter.ai/settings/keys and put it in .env")
        return 1

    end = dt.date.today() - dt.timedelta(days=1)
    if a.end:
        end = dt.date(*[int(x) for x in a.end.split("-")])
    start = max(FLOOR, dt.date(*[int(x) for x in a.start.split("-")]))

    # Probe first. This endpoint has never been exercised here, so verify it
    # answers correctly for a single day before firing 19 months of requests.
    probe_day = end.isoformat()
    print("probing %s for %s ..." % (ENDPOINT, probe_day))
    try:
        js = fetch(key, probe_day, probe_day)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        print("PROBE FAILED  HTTP %s\n  %s" % (e.code, body))
        print("\n  401/403 -> key invalid or lacks dataset access")
        print("  404     -> endpoint moved; re-check openrouter.ai/openapi.json")
        return 2
    except Exception as e:
        print("PROBE FAILED: %s" % e)
        return 2

    rows = js.get("data") or []
    if not rows:
        print("PROBE FAILED: endpoint returned no rows for %s" % probe_day)
        return 2
    sample = rows[0]
    missing = [k for k in ("date", "model_permaslug", "total_tokens") if k not in sample]
    if missing:
        print("PROBE FAILED: response is missing %s; schema changed" % missing)
        print("  got keys: %s" % list(sample.keys()))
        return 2
    print("  ok - %d rows, meta=%s" % (len(rows), js.get("meta")))
    if a.probe_only:
        return 0

    # ---- full pull, one request per calendar month
    per_day = {}
    spans = list(month_spans(start, end))
    for i, (s, e) in enumerate(spans):
        print("[%2d/%2d] %s .. %s" % (i + 1, len(spans), s, e), end=" ")
        try:
            js = fetch(key, s.isoformat(), e.isoformat())
        except Exception as ex:
            print("FAILED: %s" % str(ex)[:90])
            continue
        got = js.get("data") or []
        for r in got:
            day = r["date"][:10]
            slug = r["model_permaslug"]
            tok = float(r["total_tokens"])
            d = per_day.setdefault(day, {"total": 0.0, "us": 0.0, "models": 0})
            d["total"] += tok
            if slug != "other":
                d["models"] += 1
                if slug.split("/")[0] in US_CORE:
                    d["us"] += tok
        print("-> %d rows, %d days" % (len(got), len({r['date'][:10] for r in got})))
        if i < len(spans) - 1:
            time.sleep(a.sleep)

    if not per_day:
        print("no data pulled")
        return 1

    # Volume-only rows. Price/spend columns stay blank on purpose.
    out = []
    for day in sorted(per_day):
        d = per_day[day]
        out.append({
            "run_date": day, "window": "day", "as_of": day,
            "models": d["models"],
            "total_tokens": int(d["total"]),
            "us_tokens": int(d["us"]),
            "us_token_share_pct": round(100.0 * d["us"] / d["total"], 2) if d["total"] else "",
            "source": "backfill",
            "degraded": 0,
            "degrade_note": "volume only - top50+other from rankings-daily; no price history exists",
        })

    path = os.path.join(a.data, "llm_history_volume.csv")
    os.makedirs(a.data, exist_ok=True)
    cols = list(out[0].keys())
    with io.open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out)

    lo, hi = min(per_day), max(per_day)
    print("\nwrote %d day(s) %s .. %s -> %s" % (len(out), lo, hi, path))
    print("  latest day total: %.2fT tokens" % (per_day[hi]["total"] / 1e12))
    print("  NOTE: volume only. Price, spend and VWAP have no historical source")
    print("        and begin accumulating from your first tracker.py run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
