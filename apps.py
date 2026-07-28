# -*- coding: utf-8 -*-
"""Which applications are actually burning the tokens.

Answers the demand-side question the model-level data cannot: is growth coming
from coding agents, from chat apps, or from something new. Pulls OpenRouter's
public app marketplace rankings, overall and split by coding subcategory
(cli-agent / ide-extension / cloud-agent / programming-app).

This is the one collector that needs a key. It is free to create; the daily
seven-layer chain does not need it. Run preflight first — it reports in a few
seconds whether a zero-balance key is accepted, rather than failing opaquely
halfway through a pull.

    python apps.py --preflight
    python apps.py --out ./data
"""
import argparse, csv, datetime as dt, io, json, os, sys, time
import urllib.error, urllib.parse, urllib.request

API = "https://openrouter.ai/api/v1/datasets/app-rankings"
UA = "Mozilla/5.0 (compatible; dc-watch/1.0)"
TIMEOUT = 45

# Coding is the segment that matters for agentic-inference demand; the rest are
# collected in aggregate only.
CODING_SUBCATS = ["cli-agent", "ide-extension", "cloud-agent", "programming-app"]


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


def call(key, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(API + "?" + q, headers={
        "User-Agent": UA, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def preflight(key):
    """Verify the key works before spending the daily request budget."""
    print("preflight: %s" % API)
    try:
        js = call(key, limit=3)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        print("  FAILED  HTTP %s" % e.code)
        print("  %s" % body)
        if e.code in (401, 403):
            print("\n  -> key rejected. Either it is mistyped, or this dataset")
            print("     requires a funded account. Top up $5 and retry;")
            print("     the daily chain does not need this key at all.")
        elif e.code == 429:
            print("\n  -> rate limited (30/min, 500/day). Wait and retry.")
        elif e.code == 404:
            print("\n  -> endpoint moved. Re-check openrouter.ai/openapi.json")
        return False
    except Exception as e:
        print("  FAILED: %s" % str(e)[:200])
        return False
    rows = js.get("data") or []
    print("  OK - %d rows, as_of %s" % (len(rows), (js.get("meta") or {}).get("as_of")))
    for r in rows:
        print("     #%s %-24s %s tokens" % (r.get("rank"), r.get("app_name"),
                                            r.get("total_tokens")))
    print("\n  zero-balance key accepted. No top-up needed.")
    return True


def collect(outdir, key, window_days=7, limit=50, delay=2.5):
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=window_days - 1)
    today = dt.date.today().isoformat()
    out, calls = [], []

    calls.append(("all", None, "popular"))
    calls.append(("all", None, "trending"))
    calls.append(("coding", None, "popular"))
    for sc in CODING_SUBCATS:
        calls.append(("coding", sc, "popular"))

    for i, (cat, sub, sort) in enumerate(calls):
        try:
            js = call(key, limit=limit, sort=sort,
                      start_date=start.isoformat(), end_date=end.isoformat(),
                      category=None if cat == "all" else cat,
                      subcategory=sub)
        except Exception as e:
            print("  ! %s/%s/%s: %s" % (cat, sub, sort, str(e)[:90]))
            continue
        meta = js.get("meta") or {}
        for r in (js.get("data") or []):
            out.append({
                "run_date": today,
                "as_of": (meta.get("end_date") or end.isoformat()),
                "window_days": window_days,
                "category": cat,
                "subcategory": sub or "",
                "sort": sort,
                "rank": r.get("rank"),
                "app_id": r.get("app_id"),
                "app_name": r.get("app_name"),
                "total_tokens": r.get("total_tokens"),
                "total_requests": r.get("total_requests"),
            })
        if i < len(calls) - 1:
            time.sleep(delay)          # 30/min limit

    if out:
        os.makedirs(outdir, exist_ok=True)
        from tracker import append_csv
        append_csv(os.path.join(outdir, "app_rankings.csv"),
                   list(out[0].keys()), out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./data")
    ap.add_argument("--window", type=int, default=7)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--preflight", action="store_true")
    a = ap.parse_args()
    load_env()

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY not set.")
        print("  1. https://openrouter.ai/settings/keys -> Create Key")
        print("  2. paste it into .env as OPENROUTER_API_KEY=sk-or-...")
        print("  3. python apps.py --preflight")
        return 1

    if a.preflight:
        return 0 if preflight(key) else 2

    rows = collect(a.out, key, a.window, a.limit)
    if not rows:
        return 1
    top = [r for r in rows if r["category"] == "all" and r["sort"] == "popular"]
    print()
    print("  %-4s %-30s %14s %12s" % ("#", "app", "tokens", "requests"))
    for r in top[:15]:
        t = float(r["total_tokens"] or 0)
        print("  %-4s %-30s %13.2fT %12s"
              % (r["rank"], (r["app_name"] or "")[:30], t / 1e12, r["total_requests"]))
    print()
    print("  Source: OpenRouter (openrouter.ai/apps), as of %s." % rows[0]["as_of"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
