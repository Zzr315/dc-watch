# -*- coding: utf-8 -*-
"""
Data Center Watch replication tracker.

Rebuilds the three J.P. Morgan trackers (LLM tokens / GPU rental / memory) from
primary sources, at DAILY grain instead of JPM's one-week-per-month sample.

Run daily (cron / Task Scheduler). Each run appends one row per series to CSV,
so history accumulates even for endpoints that only expose a rolling window.

    python tracker.py --out ./data

Sources
  tokens  OpenRouter  /api/frontend/v1/rankings/models   (volume, prompt/completion split)
          OpenRouter  /api/v1/models                     (list pricing)
  memory  TrendForce  /price/dram/dram_spot              (DRAM spot, daily)
          TrendForce  /price/flash/flash_spot            (NAND spot, weekly)
  gpu     Silicon Data /api/gpu-index/index              (paid; set SD_TOKEN to enable)
"""
import argparse, csv, datetime as dt, html, io, json, os, re, sys, time, urllib.request

UA = "Mozilla/5.0 (compatible; dc-watch-tracker/1.0)"
TIMEOUT = 45

# Authors JPM counts as "U.S.-based models"
US_CORE = {"openai", "anthropic", "google", "x-ai", "meta-llama",
           "microsoft", "nvidia", "amazon"}


def load_env(path=".env"):
    """Task Scheduler does not inherit an interactive shell's environment, so
    credentials come from .env. Existing real env vars always win."""
    if not os.path.exists(path):
        return
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v and not os.environ.get(k):
                os.environ[k] = v


def get(url, headers=None, retries=2, backoff=3.0):
    """A single transient read timeout should not cost the day's collection.
    /api/v1/models is ~530 KB and does occasionally stall mid-body."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last


def append_csv(path, fieldnames, rows):
    """Append rows, reconciling schema drift. If the file on disk has a
    different header than `fieldnames`, rewrite it with the union of both
    (old columns first, new ones appended) so historical rows stay aligned
    instead of going ragged."""
    if not rows:
        return
    fieldnames = list(fieldnames)
    old_rows, old_header = [], None
    if os.path.exists(path):
        with io.open(path, encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            try:
                old_header = next(rd)
            except StopIteration:
                old_header = None
            if old_header:
                old_rows = [r for r in rd]

    if old_header is not None and old_header != fieldnames:
        merged = old_header + [c for c in fieldnames if c not in old_header]
        with io.open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=merged)
            w.writeheader()
            for r in old_rows:
                # tolerate previously-ragged rows by zipping against old_header
                w.writerow({k: v for k, v in zip(old_header, r)})
            for r in rows:
                w.writerow({k: r.get(k, "") for k in merged})
        print("  reconciled schema (%d -> %d cols), wrote %d row(s) -> %s"
              % (len(old_header), len(merged), len(rows), path))
        return

    new = old_header is None
    with io.open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if new:
            w.writeheader()
        w.writerows(rows)
    print("  wrote %d row(s) -> %s" % (len(rows), path))


# ---------------------------------------------------------------- LLM tokens
FRONTEND_RANKINGS = "https://openrouter.ai/api/frontend/v1/rankings/models?view=%s"
DOCUMENTED_RANKINGS = ("https://openrouter.ai/api/v1/datasets/rankings-daily"
                       "?period=%s&start_date=%s&end_date=%s")


def fetch_rankings(view):
    """Primary source is the undocumented frontend endpoint: it covers every
    model and splits prompt/completion tokens. It has moved once already
    (/api/frontend/models -> /api/frontend/v1/rankings/models), so on failure
    fall back to the documented dataset endpoint, which is stable but only
    returns top-50 + an aggregated `other` row and no prompt/completion split.

    Returns (rows, degraded, note)."""
    try:
        data = json.loads(get(FRONTEND_RANKINGS % view))["data"]
        if data:
            return data, False, ""
        note = "frontend endpoint returned an empty set"
    except Exception as e:
        note = "frontend endpoint failed: %s" % str(e)[:120]

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(note + "; no OPENROUTER_API_KEY set for fallback")
    span = {"day": 1, "week": 7, "month": 30}.get(view, 7)
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=span - 1)
    url = DOCUMENTED_RANKINGS % ("day", start.isoformat(), end.isoformat())
    raw = json.loads(get(url, {"Authorization": "Bearer " + key}))["data"]
    # Reshape into the frontend schema. No prompt/completion split exists here,
    # so spend cannot be estimated for these rows — leave the split at zero and
    # let the caller mark them unpriced rather than inventing a mix.
    rows = [dict(date=r["date"], model_permaslug=r["model_permaslug"],
                 variant="standard", total_prompt_tokens=int(r["total_tokens"]),
                 total_completion_tokens=0, total_native_tokens_reasoning=0,
                 count=0, total_native_tokens_cached=0, total_tool_calls=0)
            for r in raw]
    return rows, True, note + "; fell back to rankings-daily (top-50 + other, no in/out split)"


def snapshot_price_list(outdir, raw_json):
    """Archive the raw /v1/models payload, one gzipped file per day.

    The per-model CSV only keeps a *derived* blended price. That bakes today's
    methodology into history: if the blend later needs to model cache-read
    discounts, per-endpoint pricing, or a different input/output mix, the past
    cannot be recomputed. ~500 KB/day raw, ~30 MB/year gzipped, and it makes the
    whole price series re-derivable. Cheap insurance; the cost of skipping it
    compounds daily.
    """
    import gzip
    d = os.path.join(outdir, "raw", "models")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "models_%s.json.gz" % dt.date.today().isoformat())
    if os.path.exists(p):
        return p                      # already snapshotted today
    with gzip.open(p, "wb") as f:
        f.write(raw_json.encode("utf-8"))
    print("  snapshot -> %s (%.0f KB gz)" % (p, os.path.getsize(p) / 1024.0))
    return p


def llm_tokens(outdir, view="week"):
    """Figures 1-6. Joins OpenRouter volume with OpenRouter list pricing."""
    rank, degraded, degrade_note = fetch_rankings(view)
    if degraded:
        print("  ! DEGRADED: %s" % degrade_note)
    raw_models = get("https://openrouter.ai/api/v1/models")
    snapshot_price_list(outdir, raw_models)
    mods = json.loads(raw_models)["data"]

    # price book, keyed by canonical_slug (== rankings model_permaslug). USD per token.
    price = {}
    for m in mods:
        p = m.get("pricing") or {}
        pr, co = float(p.get("prompt") or 0), float(p.get("completion") or 0)
        if pr < 0 or co < 0:          # -1 sentinel on openrouter/auto router models
            continue
        price[m["canonical_slug"]] = (pr, co)

    per_model, unpriced_tok, all_tok = [], 0, 0
    for r in rank:
        ps = r["model_permaslug"]
        tp, tc = r["total_prompt_tokens"], r["total_completion_tokens"]
        tot = tp + tc
        all_tok += tot
        free = r["variant"] == "free"
        pv = price.get(ps)
        if free:
            spend, blended = 0.0, 0.0
        elif pv:
            spend = tp * pv[0] + tc * pv[1]
            blended = spend / (tot / 1e6) if tot else 0.0
        else:
            spend, blended = None, None
            unpriced_tok += tot
        per_model.append(dict(
            model=ps, author=ps.split("/")[0], variant=r["variant"],
            prompt_tokens=tp, completion_tokens=tc, total_tokens=tot,
            requests=r["count"], reasoning_tokens=r["total_native_tokens_reasoning"],
            tool_calls=r["total_tool_calls"],
            list_price_blended_usd_per_mn=round(blended, 6) if blended is not None else "",
            est_spend_usd=round(spend, 2) if spend is not None else ""))

    asof = max(r["date"][:10] for r in rank)
    priced = [m for m in per_model if m["est_spend_usd"] != "" and m["variant"] != "free"]
    tok_priced = sum(m["total_tokens"] for m in priced)
    spend = sum(m["est_spend_usd"] for m in priced)
    us = [m for m in priced if m["author"] in US_CORE]
    us_spend = sum(m["est_spend_usd"] for m in us)
    us_tok_all = sum(m["total_tokens"] for m in per_model if m["author"] in US_CORE)
    lp = [m["list_price_blended_usd_per_mn"] for m in priced
          if m["list_price_blended_usd_per_mn"]]
    sv = sorted(per_model, key=lambda x: -x["total_tokens"])
    ss = sorted(priced, key=lambda x: -x["est_spend_usd"])

    summary = dict(
        run_date=dt.date.today().isoformat(), window=view, as_of=asof,
        models=len(per_model),
        total_tokens=all_tok,
        # VWAP's denominator. Exposed explicitly because total_tokens includes
        # free-tier traffic while est_spend_usd does not — without this field a
        # reader (human or model) will wrongly conclude that free tokens are
        # dragging VWAP down. spend / paid_tokens == vwap by construction.
        paid_tokens=tok_priced,
        us_tokens=us_tok_all,
        us_token_share_pct=round(100 * us_tok_all / all_tok, 2) if all_tok else "",
        free_tokens=sum(m["total_tokens"] for m in per_model if m["variant"] == "free"),
        requests=sum(m["requests"] for m in per_model),
        output_token_share_pct=round(
            100 * sum(m["completion_tokens"] for m in per_model) / all_tok, 2) if all_tok else "",
        est_spend_usd=round(spend, 2),
        est_us_spend_usd=round(us_spend, 2),
        us_spend_share_pct=round(100 * us_spend / spend, 2) if spend else "",
        # Fig 2 = unweighted mean list price across the model list (tail-driven)
        avg_list_price_usd_per_mn=round(sum(lp) / len(lp), 4) if lp else "",
        # Fig 3 = volume-weighted (spend / tokens)
        vwap_usd_per_mn=round(spend / (tok_priced / 1e6), 4) if tok_priced else "",
        us_vwap_usd_per_mn=round(
            us_spend / (sum(m["total_tokens"] for m in us) / 1e6), 4) if us else "",
        top5_volume_share_pct=round(100 * sum(m["total_tokens"] for m in sv[:5]) / all_tok, 2) if all_tok else "",
        top5_spend_share_pct=round(100 * sum(m["est_spend_usd"] for m in ss[:5]) / spend, 2) if spend else "",
        unpriced_token_share_pct=round(100 * unpriced_tok / all_tok, 2) if all_tok else "",
        degraded=1 if degraded else 0,
        degrade_note=degrade_note)

    append_csv(os.path.join(outdir, "llm_summary.csv"), list(summary), [summary])
    detail = [dict(run_date=summary["run_date"], as_of=asof, window=view, **m)
              for m in per_model]
    # Partition by month: one appended file per month keeps each CSV small and
    # keeps git diffs from degrading as history accumulates.
    shard = "llm_by_model_%s.csv" % summary["run_date"][:7]
    append_csv(os.path.join(outdir, shard), list(detail[0]), detail)
    return summary


# ------------------------------------------------------------------- memory
def _tf_tables(url):
    h = get(url)
    stamps = re.findall(r"Last Update ([\d\-]+ [\d:]+) \(GMT\+8\)", h)
    out = []
    for tb in re.findall(r"<table.*?</table>", h, flags=re.S):
        rows = []
        for r in re.findall(r"<tr.*?</tr>", tb, flags=re.S):
            cl = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                  for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, flags=re.S)]
            cl = [c for c in cl if c]
            if cl:
                rows.append(cl)
        if rows:
            out.append(rows)
    return out, stamps


def memory(outdir):
    """Figures 9-10. DRAM spot is daily; NAND spot is weekly."""
    rows = []
    for kind, url, want in [
        ("dram", "https://www.trendforce.com/price/dram/dram_spot",
         ("DDR5 16Gb (2Gx8) 4800/5600", "DDR5 16Gb (2Gx8) eTT",
          "DDR4 16Gb (2Gx8) 3200", "DDR4 8Gb (1Gx8) 3200")),
        ("nand", "https://www.trendforce.com/price/flash/flash_spot",
         ("512Gb TLC", "256Gb TLC", "128Gb TLC")),
    ]:
        try:
            tables, stamps = _tf_tables(url)
        except Exception as e:
            print("  ! %s fetch failed: %s" % (kind, e))
            continue
        stamp = stamps[0] if stamps else ""
        for tb in tables:
            hdr = tb[0]
            try:
                avg_i = next(i for i, c in enumerate(hdr) if "Average" in c and "Change" not in c)
            except StopIteration:
                continue
            for r in tb[1:]:
                if r[0] in want and len(r) > avg_i:
                    val = r[avg_i].replace(",", "")
                    chg = next((c for c in r if "%" in c), "")
                    try:
                        val = float(val)
                    except ValueError:
                        continue
                    rows.append(dict(run_date=dt.date.today().isoformat(),
                                     source_update=stamp, kind=kind, item=r[0],
                                     session_avg_usd=val,
                                     session_change=re.sub(r"[^\d\.\-%]", "", chg)))
    # de-dup (same item can appear in >1 table)
    seen, ded = set(), []
    for r in rows:
        k = (r["kind"], r["item"])
        if k not in seen:
            seen.add(k); ded.append(r)
    if ded:
        append_csv(os.path.join(outdir, "memory_spot.csv"), list(ded[0]), ded)
    return ded


# ---------------------------------------------------------------- GPU rental
def gpu_rental(outdir, days=7):
    """Figures 7-8. Silicon Data GPU Index (Bloomberg/LSEG-distributed).
    Requires a bearer token in env SD_TOKEN. index_version 'neo' == non-hyperscaler."""
    tok = os.environ.get("SD_TOKEN")
    if not tok:
        print("  - skipped: set SD_TOKEN to enable (paid Plus/Professional tier).")
        print("    free proxies: sfcompute.com/prices, vast.ai bundles API (key), runpod/lambda list pages")
        return []
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    rows = []
    for gpu in ("a100", "h100", "b200", "h200", "mi300x"):
        body = json.dumps(dict(gpu=gpu, index_version="neo",
                               starting_date=start.isoformat(),
                               ending_date=end.isoformat())).encode()
        req = urllib.request.Request(
            "https://api.silicondata.com/api/gpu-index/index", data=body,
            headers={"Authorization": "Bearer " + tok,
                     "Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode())["data"]
        except Exception as e:
            print("  ! %s: %s" % (gpu, e)); continue
        for date, v in sorted((d.get("indexes") or {}).items()):
            if float(v) < 0:      # -1 == not yet generated
                continue
            rows.append(dict(date=date, gpu=gpu.upper(),
                             index_version=d.get("index_version", "neo"),
                             usd_per_gpu_hour=float(v)))
    if rows:
        append_csv(os.path.join(outdir, "gpu_rental.csv"), list(rows[0]), rows)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./data")
    ap.add_argument("--view", default="day,week",
                    help="comma-separated windows to collect: day, week, month. "
                         "Default collects both day (clean daily series) and "
                         "week (smoothed, comparable to the JPM weekly metric).")
    ap.add_argument("--only", default="all",
                    choices=["all", "tokens", "memory", "gpu"])
    a = ap.parse_args()
    load_env()
    os.makedirs(a.out, exist_ok=True)

    if a.only in ("all", "tokens"):
        for view in [v.strip() for v in a.view.split(",") if v.strip()]:
            print("[tokens/%s] OpenRouter volume x pricing ..." % view)
            s = llm_tokens(a.out, view)
            print("  %s to %s: %.1fT tokens | $%.1fM spend | VWAP $%.2f/Mn | US %s%% vol / %s%% spend"
                  % (view, s["as_of"], s["total_tokens"] / 1e12, s["est_spend_usd"] / 1e6,
                     s["vwap_usd_per_mn"], s["us_token_share_pct"], s["us_spend_share_pct"]))
    if a.only in ("all", "memory"):
        print("[memory] TrendForce spot ...")
        for r in memory(a.out):
            print("  %-30s $%9.3f  %s  (upd %s)"
                  % (r["item"], r["session_avg_usd"], r["session_change"], r["source_update"]))
    if a.only in ("all", "gpu"):
        print("[gpu] Silicon Data GPU Index ...")
        gpu_rental(a.out)


if __name__ == "__main__":
    sys.exit(main())
