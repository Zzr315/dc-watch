# -*- coding: utf-8 -*-
"""Turn the accumulated CSV history into a change brief.

Reads everything under data/ and emits state/brief.json: current levels,
d/d - w/w - m/m - y/y deltas, biggest movers, top-20 entries and exits,
statistical anomalies, and explicit data-gap dates.

The brief is both what the dashboard renders and what Opus 5 is handed to
write commentary from, so it must be small, self-describing, and honest about
what is missing rather than silently interpolating.

    python analyze.py --data ./data --out ./state/brief.json
"""
import argparse, csv, datetime as dt, glob, io, json, os, statistics

# how far a lookup may drift from the requested offset before giving up
TOLERANCE = {"dod": 2, "wow": 3, "mom": 6, "yoy": 14}
OFFSETS = {"dod": 1, "wow": 7, "mom": 30, "yoy": 365}


def read_csv(path):
    if not os.path.exists(path):
        return []
    with io.open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def dedupe(rows, key):
    """Re-running the tracker in one day appends a second row; keep the last."""
    out = {}
    for r in rows:
        out[key(r)] = r
    return list(out.values())


def d(s):
    return dt.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))


def nearest(series, target, tol):
    """series: {date -> value}. Return value at the date closest to target
    within tol days, else None."""
    best, bestgap = None, None
    for k, v in series.items():
        gap = abs((d(k) - target).days)
        if gap <= tol and (bestgap is None or gap < bestgap):
            best, bestgap = v, gap
    return best


def deltas(series):
    """series: {date -> float}. Returns level plus pct changes vs each offset."""
    if not series:
        return None
    latest_date = max(series, key=d)
    cur = series[latest_date]
    out = {"date": latest_date, "value": cur}
    for name, off in OFFSETS.items():
        prev = nearest(series, d(latest_date) - dt.timedelta(days=off), TOLERANCE[name])
        if prev is None or prev == 0 or cur is None:
            out[name] = None
        else:
            out[name] = round(100.0 * (cur - prev) / abs(prev), 2)
    return out


def anomalies(series, label, min_obs=10, z=2.0):
    """Flag the latest day-over-day move if it is >z sigma of recent moves."""
    dates = sorted(series, key=d)
    if len(dates) < min_obs + 1:
        return None
    chg = []
    for a, b in zip(dates, dates[1:]):
        pa, pb = series[a], series[b]
        if pa:
            chg.append(100.0 * (pb - pa) / abs(pa))
    if len(chg) < min_obs:
        return None
    latest, hist = chg[-1], chg[:-1]
    try:
        mu, sd = statistics.mean(hist), statistics.pstdev(hist)
    except statistics.StatisticsError:
        return None
    if not sd:
        return None
    score = (latest - mu) / sd
    if abs(score) < z:
        return None
    return {"metric": label, "change_pct": round(latest, 2),
            "z_score": round(score, 2),
            "baseline_mean_pct": round(mu, 2), "baseline_sd_pct": round(sd, 2)}


def find_gaps(dates):
    """Missing calendar days between first and last observation. The frontend
    rankings endpoint only exposes a rolling window, so a gap is permanent —
    the dashboard must draw a break, not a straight line across it."""
    if len(dates) < 2:
        return []
    ds = sorted(d(x) for x in dates)
    gaps, cur = [], ds[0]
    while cur < ds[-1]:
        cur += dt.timedelta(days=1)
        if cur not in ds and cur != ds[-1]:
            gaps.append(cur.isoformat())
    return gaps


# --------------------------------------------------------------------- LLM
LLM_METRICS = [
    ("total_tokens", "Total weekly tokens (incl. free tier)", "tokens"),
    ("paid_tokens", "Paid tokens (VWAP denominator)", "tokens"),
    ("us_tokens", "U.S.-model tokens", "tokens"),
    ("us_token_share_pct", "U.S. share of volume", "pct"),
    ("est_spend_usd", "Estimated spend", "usd"),
    ("est_us_spend_usd", "Estimated U.S. spend", "usd"),
    ("us_spend_share_pct", "U.S. share of spend", "pct"),
    ("vwap_usd_per_mn", "VWAP (realized mix)", "usd_per_mn"),
    ("avg_list_price_usd_per_mn", "Unweighted avg list price", "usd_per_mn"),
    ("us_vwap_usd_per_mn", "U.S. VWAP", "usd_per_mn"),
    ("requests", "Requests", "count"),
    ("output_token_share_pct", "Output-token share", "pct"),
    ("top5_volume_share_pct", "Top-5 volume concentration", "pct"),
    ("top5_spend_share_pct", "Top-5 spend concentration", "pct"),
]


def sdate(r):
    """Series date is the date the data describes, not the date we fetched it.
    A live row is written the morning after (run_date=today, as_of=yesterday);
    a backfilled row is dated directly. Keying on as_of puts both on one axis."""
    return r.get("as_of") or r["run_date"]


def llm_section(datadir, window="week"):
    rows = [r for r in read_csv(os.path.join(datadir, "llm_summary.csv"))
            if r.get("window") == window]
    if window == "day":
        # volume-only history from backfill.py; live rows win on collision
        hist = read_csv(os.path.join(datadir, "llm_history_volume.csv"))
        live = {sdate(r) for r in rows}
        rows = [h for h in hist if sdate(h) not in live] + rows
    rows = dedupe(rows, sdate)
    if not rows:
        return None, [], []
    rows.sort(key=sdate)

    metrics, anoms = {}, []
    for key, label, unit in LLM_METRICS:
        series = {}
        for r in rows:
            v = num(r.get(key))
            if v is not None:
                series[sdate(r)] = v
        dl = deltas(series)
        if dl:
            dl["label"], dl["unit"] = label, unit
            dl["history"] = [[k, series[k]] for k in sorted(series, key=d)]
            metrics[key] = dl
        a = anomalies(series, label)
        if a:
            anoms.append(a)

    latest = rows[-1]
    meta = {"window": window, "run_date": latest["run_date"],
            "as_of": sdate(latest),
            "observations": len(rows),
            "first_run": sdate(rows[0]),
            "gaps": find_gaps([sdate(r) for r in rows]),
            "degraded": latest.get("degraded") == "1",
            "degrade_note": latest.get("degrade_note", ""),
            "models_tracked": int(num(latest.get("models")) or 0),
            "unpriced_token_share_pct": num(latest.get("unpriced_token_share_pct"))}
    return meta, metrics, anoms


def model_movers(datadir, window="week", top=8):
    """Compare the latest per-model snapshot against roughly a week earlier."""
    rows = []
    for p in sorted(glob.glob(os.path.join(datadir, "llm_by_model_*.csv"))):
        rows.extend([r for r in read_csv(p) if r.get("window") == window])
    if not rows:
        return {}
    dates = sorted({r["run_date"] for r in rows})
    cur_date = dates[-1]
    prev_date = None
    for cand in reversed(dates[:-1]):
        if (d(cur_date) - d(cand)).days >= 6:
            prev_date = cand
            break
    if prev_date is None and len(dates) > 1:
        prev_date = dates[0]

    def snap(dd):
        out = {}
        for r in rows:
            if r["run_date"] != dd:
                continue
            k = r["model"] + ("::free" if r.get("variant") == "free" else "")
            out[k] = {"tokens": num(r.get("total_tokens")) or 0.0,
                      "spend": num(r.get("est_spend_usd")) or 0.0,
                      "price": num(r.get("list_price_blended_usd_per_mn")),
                      "author": r.get("author", "")}
        return out

    cur = snap(cur_date)
    prev = snap(prev_date) if prev_date else {}

    def rank(sn, field, n=20):
        return [k for k, _ in sorted(sn.items(), key=lambda kv: -kv[1][field])[:n]]

    out = {"current_date": cur_date, "compare_date": prev_date,
           "top20_volume": [{"model": k, "tokens": cur[k]["tokens"],
                             "spend": cur[k]["spend"], "price": cur[k]["price"]}
                            for k in rank(cur, "tokens")],
           "top20_spend": [{"model": k, "spend": cur[k]["spend"],
                            "tokens": cur[k]["tokens"], "price": cur[k]["price"]}
                           for k in rank(cur, "spend")]}
    if not prev:
        out["note"] = "no prior snapshot yet - movers unavailable"
        return out

    moves = []
    for k, v in cur.items():
        p = prev.get(k)
        if not p or not p["tokens"]:
            continue
        if v["tokens"] < 5e10 and p["tokens"] < 5e10:   # ignore sub-50B noise
            continue
        moves.append({"model": k, "author": v["author"],
                      "tokens_now": v["tokens"], "tokens_then": p["tokens"],
                      "tokens_chg_pct": round(100.0 * (v["tokens"] - p["tokens"]) / p["tokens"], 1),
                      "spend_chg_usd": round(v["spend"] - p["spend"], 2)})
    moves.sort(key=lambda m: -m["tokens_chg_pct"])
    out["volume_gainers"] = moves[:top]
    out["volume_losers"] = moves[-top:][::-1]
    cur20, prev20 = set(rank(cur, "tokens")), set(rank(prev, "tokens"))
    out["entered_top20"] = sorted(cur20 - prev20)
    out["left_top20"] = sorted(prev20 - cur20)
    new = sorted(set(cur) - set(prev),
                 key=lambda k: -cur[k]["tokens"])[:top]
    out["new_models"] = [{"model": k, "tokens": cur[k]["tokens"]} for k in new]
    return out


# ------------------------------------------------------------------ memory
def memory_section(datadir):
    rows = read_csv(os.path.join(datadir, "memory_spot.csv"))
    if not rows:
        return {}
    rows = dedupe(rows, lambda r: (r["run_date"], r["kind"], r["item"]))
    items = {}
    for r in rows:
        v = num(r.get("session_avg_usd"))
        if v is None:
            continue
        it = items.setdefault(r["item"], {"kind": r["kind"], "series": {},
                                          "source_update": r.get("source_update", "")})
        it["series"][r["run_date"]] = v
        it["source_update"] = r.get("source_update", it["source_update"])

    out = {}
    for name, it in items.items():
        dl = deltas(it["series"])
        if not dl:
            continue
        ordered = sorted(it["series"], key=d)
        base = it["series"][ordered[0]] or None
        dl["kind"] = it["kind"]
        # DRAM spot updates daily; NAND spot only weekly. Carry the cadence so
        # the dashboard can label it instead of implying equal frequency.
        dl["cadence"] = "daily" if it["kind"] == "dram" else "weekly"
        dl["source_update"] = it["source_update"]
        dl["history"] = [[k, it["series"][k]] for k in ordered]
        dl["indexed"] = ([[k, round(100.0 * it["series"][k] / base, 2)] for k in ordered]
                         if base else [])
        out[name] = dl
    return out


# --------------------------------------------------------------------- GPU
def shadeform_section(datadir):
    """Free stand-in for the Silicon Data index: median $/GPU-hour and the
    share of regions with stock, across ~18 neoclouds."""
    rows = read_csv(os.path.join(datadir, "gpu_shadeform.csv"))
    if not rows:
        return {}
    rows = dedupe(rows, lambda r: (r["run_date"], r["gpu"]))
    out = {}
    for gpu in sorted({r["gpu"] for r in rows}):
        px, av = {}, {}
        for r in rows:
            if r["gpu"] != gpu:
                continue
            p = num(r.get("usd_per_gpu_hour_median"))
            a = num(r.get("availability_pct"))
            if p is not None:
                px[r["run_date"]] = p
            if a is not None:
                av[r["run_date"]] = a
        dl = deltas(px)
        if not dl:
            continue
        latest = max(px, key=d)
        cur = [r for r in rows if r["gpu"] == gpu and r["run_date"] == latest][0]
        dl.update({
            "history": [[k, px[k]] for k in sorted(px, key=d)],
            "availability_pct": av.get(latest),
            "availability_history": [[k, av[k]] for k in sorted(av, key=d)],
            "availability_delta": deltas(av).get("dod") if av else None,
            "min": num(cur.get("usd_per_gpu_hour_min")),
            "max": num(cur.get("usd_per_gpu_hour_max")),
            "clouds": int(num(cur.get("clouds")) or 0),
            "offers": int(num(cur.get("offers")) or 0),
        })
        out[gpu] = dl
    return out


# ------------------------------------------------------------------ bridge
def bridge_section(datadir, gpu_ref="H100"):
    """Break-even concurrency: how many simultaneous streams one GPU-equivalent
    must serve before token revenue covers its rental cost.

        revenue per stream-hour = tok_s * 3600 * price_per_output_token
        break_even              = gpu_usd_per_hour / revenue_per_stream_hour

    Deliberately inverted. Computing a cost per token instead would require
    assuming a batch size, which is unobservable and would dominate the answer
    — the same class of hidden assumption that makes the sell-side spend
    estimates wobble. This form assumes nothing about batching; it states the
    concurrency required, and the reader compares it to what real serving
    achieves (typically tens to hundreds of streams).

    Caveat carried in the output: normalised per GPU-equivalent, not per
    replica. A large MoE needs 8+ GPUs for one replica, so its true break-even
    is roughly this number times the GPUs per replica.
    """
    perf = read_csv(os.path.join(datadir, "model_perf.csv"))
    gpus = read_csv(os.path.join(datadir, "gpu_shadeform.csv"))
    if not perf or not gpus:
        return {}
    perf = dedupe(perf, lambda r: (r["run_date"], r["model"], r.get("variant", "")))
    latest_p = max(r["run_date"] for r in perf)

    # replica size, where it is knowable at all
    reps = read_csv(os.path.join(datadir, "model_replicas.csv"))
    reps = dedupe(reps, lambda r: (r["run_date"], r["model"], r.get("variant", "")))
    rep_by = {}
    if reps:
        rl = max(r["run_date"] for r in reps)
        for r in reps:
            if r["run_date"] == rl:
                rep_by[(r["model"], r.get("variant", ""))] = r

    gref = [r for r in gpus if r["gpu"] == gpu_ref]
    if not gref:
        return {}
    latest_g = max(r["run_date"] for r in gref)
    gp = num([r for r in gref if r["run_date"] == latest_g][0]
             .get("usd_per_gpu_hour_median"))
    if not gp:
        return {}

    models = []
    for r in perf:
        if r["run_date"] != latest_p:
            continue
        t = num(r.get("tok_s_weighted"))
        px = num(r.get("out_price_usd_per_mn_median"))
        if not t or not px:
            continue
        rev = t * 3600.0 * (px / 1e6)          # $ per stream-hour
        if rev <= 0:
            continue
        rp = rep_by.get((r["model"], r.get("variant", ""))) or {}
        ngpu = num(rp.get("gpus_per_replica"))
        per_gpu = gp / rev
        models.append({
            "model": r["model"],
            "tok_s": t,
            "out_price_usd_per_mn": px,
            "revenue_per_stream_hour": round(rev, 4),
            "break_even_streams": round(per_gpu, 2),
            # per-replica is the number that actually matters; it exists only for
            # open-weight models, since closed ones publish no parameter count
            "gpus_per_replica": int(ngpu) if ngpu else None,
            "params_b": round(num(rp.get("params")) / 1e9, 0) if num(rp.get("params")) else None,
            "quantization": rp.get("quantization") or "",
            "break_even_streams_replica": round(per_gpu * ngpu, 1) if ngpu else None,
            "replica_status": rp.get("status", "unknown"),
        })
    if not models:
        return {}
    models.sort(key=lambda m: m["break_even_streams"])
    vals = [m["break_even_streams"] for m in models]
    rvals = [m["break_even_streams_replica"] for m in models
             if m["break_even_streams_replica"]]
    return {
        "gpu_ref": gpu_ref,
        "gpu_usd_per_hour": gp,
        "as_of": latest_p,
        "models": models,
        "median_break_even_streams": round(statistics.median(vals), 2),
        "median_break_even_replica": round(statistics.median(rvals), 1) if rvals else None,
        "n_with_replica": len(rvals),
        "easiest": models[0]["model"],
        "hardest": models[-1]["model"],
        "note": ("每 GPU 等效数按单卡归一；每副本数已用真实参数量与服务精度折算"
                 "（HuggingFace safetensors × OpenRouter quantization 字段）。"
                 "闭源模型不公开参数量，只有每 GPU 数。"
                 "分子只计输出 token 收入，而输入 token 是其约 29 倍的量且单独计费，"
                 "因此系统性低估真实单流创收；价格用列表价，不含企业折扣与缓存折扣。"),
    }


# --------------------------------------------------- AI infra tightness index
# Each component maps to a direction that means "supply/demand is tightening".
# Sign is +1 when a rise means tighter, -1 when a fall means tighter.
DIFFUSION_COMPONENTS = [
    ("llm.total_tokens",        +1, "Token 需求",   "需求", "OpenRouter 用量", "用得越多越紧"),
    ("llm.requests",            +1, "请求数",       "需求", "OpenRouter 用量", "请求越多越紧"),
    ("llm.vwap_usd_per_mn",     +1, "成交单价",     "定价", "用量 x 价目表", "卖得越贵越紧"),
    ("gpu.H100",                +1, "H100 租金",    "算力", "Shadeform 跨云中位", "租金越高越紧"),
    ("gpu.B200",                +1, "B200 租金",    "算力", "Shadeform 跨云中位", "租金越高越紧"),
    ("avail.H100",              -1, "H100 可用率",  "供给", "Shadeform 区域有货率", "有货越少越紧"),
    ("avail.A100",              -1, "A100 可用率",  "供给", "Shadeform 区域有货率", "有货越少越紧"),
    ("mem.DDR5 16Gb (2Gx8) 4800/5600", +1, "DDR5 现货", "成本", "TrendForce 现货(日)", "涨价越多越紧"),
    ("mem.512Gb TLC",           +1, "NAND 现货",    "成本", "TrendForce 现货(周)", "涨价越多越紧"),
]


def diffusion_index(metrics, gpu_market, memory):
    """Share of components moving toward tightening, 0-100. 50 is neutral.

    A diffusion index rather than a z-score composite on purpose. A z-score
    needs a stable mean and standard deviation; with a handful of observations
    both are noise, and the resulting index swings wildly for no reason. Signs
    of change are well defined from the second observation onward, so this is
    readable immediately and converges rather than thrashing.

    Weights are equal. Any other choice would be an unstated opinion about
    which link in the chain matters most.
    """
    comps = []
    for key, sign, label, layer, source, rule in DIFFUSION_COMPONENTS:
        kind, name = key.split(".", 1)
        chg = None
        if kind == "llm":
            chg = (metrics.get(name) or {}).get("dod")
        elif kind == "gpu":
            chg = (gpu_market.get(name) or {}).get("dod")
        elif kind == "avail":
            chg = (gpu_market.get(name) or {}).get("availability_delta")
        elif kind == "mem":
            chg = (memory.get(name) or {}).get("dod")
        base = {"label": label, "layer": layer, "source": source, "rule": rule,
                "inverted": sign < 0}
        if chg is None:
            base.update({"change_pct": None, "direction": "n/a", "tightening": None})
            comps.append(base)
            continue
        eff = chg * sign
        base.update({"change_pct": round(chg, 2),
                     "direction": "紧张" if eff > 0 else ("宽松" if eff < 0 else "持平"),
                     "tightening": True if eff > 0 else (False if eff < 0 else None)})
        comps.append(base)

    live = [c for c in comps if c["change_pct"] is not None]
    if not live:
        return {"available": False, "components": comps,
                "note": "尚无可比观测，需至少 2 次采集"}
    up = sum(1 for c in live if c["tightening"] is True)
    flat = sum(1 for c in live if c["tightening"] is None)
    score = 100.0 * (up + 0.5 * flat) / len(live)
    return {
        "available": True,
        "value": round(score, 1),
        "components": comps,
        "n_live": len(live),
        "n_total": len(comps),
        "tightening": up,
        "easing": len(live) - up - flat,
        "flat": flat,
        "note": ("等权扩散指数：各分项朝紧张方向变动的占比。50 为中性。"
                 "刻意不用 z-score 合成——观测数少时均值与标准差本身就是噪声，"
                 "会让指数无理由地剧烈摆动。"),
    }


def market_share_section(datadir, top=10):
    """Vendor-level weekly share with a year of history behind it.

    This is the only series in the dashboard that arrives pre-loaded with 52
    completed weeks, so it is the only place year-over-year structure can be
    read today rather than a month from now."""
    rows = read_csv(os.path.join(datadir, "market_share.csv"))
    if not rows:
        return {}
    weeks = sorted({r["week"] for r in rows})
    if len(weeks) < 2:
        return {}
    first, last = weeks[0], weeks[-1]
    by_week = {}
    for r in rows:
        by_week.setdefault(r["week"], {})[r["author"]] = r

    cur, old = by_week[last], by_week[first]
    prev_wk = weeks[-2]
    prev = by_week.get(prev_wk, {})

    def sh(d, a):
        return num((d.get(a) or {}).get("share_pct")) or 0.0

    authors = sorted({r["author"] for r in rows},
                     key=lambda a: -sh(cur, a))
    ranked = [a for a in authors if a != "others"][:top]

    # every vendor's full year, not just the top few — the dashboard renders
    # these as small multiples so there is no palette limit to respect
    series = {}
    for a in authors:
        series[a] = [[w, sh(by_week.get(w, {}), a)] for w in weeks]

    movers = []
    for a in authors:
        if a == "others":
            continue
        movers.append({"author": a, "share_pct": round(sh(cur, a), 2),
                       "share_1y_ago": round(sh(old, a), 2),
                       "chg_1y_pct_points": round(sh(cur, a) - sh(old, a), 2),
                       "chg_1w_pct_points": round(sh(cur, a) - sh(prev, a), 2)})
    movers.sort(key=lambda m: -abs(m["chg_1y_pct_points"]))

    us_now = sum(sh(cur, a) for a in cur if (cur[a].get("is_us") == "1"))
    us_then = sum(sh(old, a) for a in old if (old[a].get("is_us") == "1"))
    # two aggregate lines rather than 19 vendor lines: the categorical palette
    # tops out well before 19 series, and this is the actual story anyway
    us_series, rest_series = [], []
    for w in weeks:
        d = by_week.get(w, {})
        u = sum(sh(d, a) for a in d if d[a].get("is_us") == "1")
        us_series.append([w, round(u, 2)])
        rest_series.append([w, round(100.0 - u, 2)])
    return {
        "available": True, "weeks": len(weeks), "first": first, "last": last,
        "top": ranked, "series": series, "authors": authors,
        "movers": movers,
        "us_share_now": round(us_now, 2), "us_share_1y_ago": round(us_then, 2),
        "us_series": us_series, "rest_series": rest_series,
        "residual_pct": round(sh(cur, "others"), 2),
    }


def apps_section(datadir):
    """Which applications burn the tokens. Empty until an OPENROUTER_API_KEY
    is configured — this is the one collector that needs a key."""
    rows = read_csv(os.path.join(datadir, "app_rankings.csv"))
    if not rows:
        return {"available": False,
                "note": ("需要免费的 OPENROUTER_API_KEY。运行 "
                         "`python apps.py --preflight` 验证后即可采集。")}
    rows = dedupe(rows, lambda r: (r["run_date"], r["category"],
                                   r.get("subcategory", ""), r["sort"], r["rank"]))
    latest = max(r["run_date"] for r in rows)
    cur = [r for r in rows if r["run_date"] == latest]

    def slice_(cat, sub, sort):
        s = [r for r in cur if r["category"] == cat
             and (r.get("subcategory") or "") == (sub or "") and r["sort"] == sort]
        s.sort(key=lambda r: int(num(r.get("rank")) or 999))
        return [{"rank": int(num(r["rank"]) or 0), "app": r["app_name"],
                 "tokens": num(r.get("total_tokens")),
                 "requests": num(r.get("total_requests"))} for r in s[:20]]

    overall = slice_("all", None, "popular")
    total = sum(a["tokens"] or 0 for a in overall)
    return {
        "available": True, "as_of": cur[0].get("as_of", latest),
        "window_days": int(num(cur[0].get("window_days")) or 0),
        "overall": overall,
        "trending": slice_("all", None, "trending"),
        "coding": slice_("coding", None, "popular"),
        "by_subcategory": {sc: slice_("coding", sc, "popular")
                           for sc in ("cli-agent", "ide-extension",
                                      "cloud-agent", "programming-app")},
        "top10_share_pct": (round(100.0 * sum(a["tokens"] or 0 for a in overall[:10]) / total, 1)
                            if total else None),
    }


def equity_section(datadir):
    """Price action of the AI-infra complex, grouped by chain position.
    No consensus or revision data — that needs a paid terminal."""
    rows = read_csv(os.path.join(datadir, "equity.csv"))
    if not rows:
        return {"available": False}
    # re-running on the same day appends a second set; keep the last per ticker
    rows = dedupe(rows, lambda r: (r["run_date"], r["ticker"]))
    latest = max(r["run_date"] for r in rows)
    cur = [r for r in rows if r["run_date"] == latest]
    names = []
    for r in cur:
        names.append({
            "ticker": r["ticker"], "group": r["group"],
            "price": num(r.get("price")),
            "chg_1d_pct": num(r.get("chg_1d_pct")),
            "chg_1mo_pct": num(r.get("chg_1mo_pct")),
            "pct_below_52w_high": num(r.get("pct_below_52w_high")),
        })
    groups = {}
    for n in names:
        groups.setdefault(n["group"], []).append(n)
    gsum = {}
    for g, ns in groups.items():
        d1 = [x["chg_1d_pct"] for x in ns if x["chg_1d_pct"] is not None]
        dm = [x["chg_1mo_pct"] for x in ns if x["chg_1mo_pct"] is not None]
        bh = [x["pct_below_52w_high"] for x in ns if x["pct_below_52w_high"] is not None]
        gsum[g] = {
            "n": len(ns),
            "chg_1d_pct": round(statistics.mean(d1), 2) if d1 else None,
            "chg_1mo_pct": round(statistics.mean(dm), 2) if dm else None,
            "pct_below_52w_high": round(statistics.mean(bh), 1) if bh else None,
        }
    names.sort(key=lambda x: (x["group"], x["ticker"]))
    return {"available": True, "as_of": latest, "names": names,
            "groups": gsum,
            "note": "仅价格与 52 周区间；Consensus / Revision 需付费终端，未纳入。"}


def hyperscaler_section(datadir):
    """Azure retail list prices — the hyperscaler tier. Three tiers are kept
    separate on purpose: on-demand list, evictable Low Priority, and Spot differ
    by ~5x, and a negotiated enterprise contract (not public anywhere) sits
    between them. Quoting "the hyperscaler price" without the tier is meaningless."""
    rows = read_csv(os.path.join(datadir, "gpu_hyperscaler.csv"))
    if not rows:
        return {}
    rows = dedupe(rows, lambda r: (r["run_date"], r["gpu"]))
    out = {}
    for gpu in sorted({r["gpu"] for r in rows}):
        series = {t: {} for t in ("ondemand", "lowpri", "spot")}
        for r in rows:
            if r["gpu"] != gpu:
                continue
            for t in series:
                v = num(r.get("%s_median" % t))
                if v is not None:
                    series[t][r["run_date"]] = v
        od = deltas(series["ondemand"])
        if not od:
            continue
        latest = max(series["ondemand"], key=d)
        cur = [r for r in rows if r["gpu"] == gpu and r["run_date"] == latest][0]
        od.update({
            "history": [[k, series["ondemand"][k]] for k in sorted(series["ondemand"], key=d)],
            "lowpri": series["lowpri"].get(latest),
            "spot": series["spot"].get(latest),
            "regions": int(num(cur.get("regions")) or 0),
            "sku": cur.get("sku", ""),
        })
        out[gpu] = od
    return out


def gpu_section(datadir):
    rows = read_csv(os.path.join(datadir, "gpu_rental.csv"))
    if not rows:
        return {"status": "awaiting_sd_token",
                "note": ("Silicon Data GPU Index (Bloomberg/LSEG-distributed, "
                         "index_version=neo = non-hyperscaler) requires a paid "
                         "Plus/Professional subscription. Set SD_TOKEN to enable.")}
    out = {"status": "live", "gpus": {}}
    for gpu in sorted({r["gpu"] for r in rows}):
        series = {}
        for r in rows:
            if r["gpu"] != gpu:
                continue
            v = num(r.get("usd_per_gpu_hour"))
            if v is not None:
                series[r["date"]] = v
        dl = deltas(series)
        if dl:
            dl["history"] = [[k, series[k]] for k in sorted(series, key=d)]
            out["gpus"][gpu] = dl
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./data")
    ap.add_argument("--out", default="./state/brief.json")
    ap.add_argument("--window", default="week", choices=["day", "week", "month"])
    a = ap.parse_args()

    meta, metrics, anoms = llm_section(a.data, a.window)
    if meta is None:
        print("no LLM history yet - run tracker.py first")
        meta, metrics, anoms = {"window": a.window, "observations": 0}, {}, []

    day_meta, day_metrics, day_anoms = llm_section(a.data, "day")
    mem = memory_section(a.data)
    gpu_mkt = shadeform_section(a.data)
    for name, m in mem.items():
        aa = anomalies({k: v for k, v in m["history"]}, "memory: " + name)
        if aa:
            anoms.append(aa)

    brief = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "llm": {"meta": meta, "metrics": metrics,
                "daily": {"meta": day_meta or {}, "metrics": day_metrics or {}},
                "models": model_movers(a.data, a.window)},
        "memory": mem,
        "gpu": gpu_section(a.data),
        "gpu_market": gpu_mkt,
        "hyperscaler": hyperscaler_section(a.data),
        "bridge": bridge_section(a.data),
        "apps": apps_section(a.data),
        "market_share": market_share_section(a.data),
        "equity": equity_section(a.data),
        "tightness": diffusion_index(metrics, gpu_mkt, mem),
        "anomalies": anoms,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8") as f:
        f.write(json.dumps(brief, ensure_ascii=False, indent=1))

    print("brief -> %s" % a.out)
    print("  window=%s  observations=%d  history from %s"
          % (meta.get("window"), meta.get("observations", 0), meta.get("first_run", "n/a")))
    if meta.get("gaps"):
        print("  ! %d data gap day(s), first: %s" % (len(meta["gaps"]), meta["gaps"][0]))
    print("  metrics=%d  memory items=%d  anomalies=%d  gpu=%s"
          % (len(metrics), len(mem), len(anoms), brief["gpu"].get("status")))
    gm = brief["gpu_market"]
    if gm:
        print("  gpu market: %d types | %s" % (len(gm), ", ".join(
            "%s $%.2f/%s%%" % (g, v["value"], v.get("availability_pct"))
            for g, v in sorted(gm.items())[:5])))
    br = brief["bridge"]
    if br:
        print("  bridge: %s @ $%.2f/hr | median break-even %.1f streams (%d models)"
              % (br["gpu_ref"], br["gpu_usd_per_hour"],
                 br["median_break_even_streams"], len(br["models"])))
    ti = brief["tightness"]
    if ti.get("available"):
        print("  tightness: %.1f/100  (紧张 %d / 宽松 %d / 持平 %d, %d 分项)"
              % (ti["value"], ti["tightening"], ti["easing"], ti["flat"], ti["n_live"]))
    else:
        print("  tightness: n/a - %s" % ti.get("note", ""))
    eq = brief["equity"]
    if eq.get("available"):
        print("  equity: %d names | %s" % (len(eq["names"]), ", ".join(
            "%s %+.1f%%" % (g, v["chg_1d_pct"]) for g, v in sorted(eq["groups"].items())
            if v["chg_1d_pct"] is not None)))
    ms = brief["market_share"]
    if ms.get("available"):
        print("  market share: %d 周 (%s..%s) | 美国系 %.1f%% -> %.1f%% | 最大变动 %s %+.1fpct"
              % (ms["weeks"], ms["first"], ms["last"], ms["us_share_1y_ago"],
                 ms["us_share_now"], ms["movers"][0]["author"],
                 ms["movers"][0]["chg_1y_pct_points"]))
    ap_ = brief["apps"]
    print("  apps: %s" % ("%d 应用, top10 占 %s%%" % (len(ap_["overall"]), ap_["top10_share_pct"])
                          if ap_.get("available") else ap_.get("note", "n/a")))
    for x in anoms:
        print("    anomaly: %s %+.1f%% (z=%.1f)" % (x["metric"], x["change_pct"], x["z_score"]))


if __name__ == "__main__":
    main()
