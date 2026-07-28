# -*- coding: utf-8 -*-
"""Rebuild the J.P. Morgan Data Center Watch charts from primary data.

Reads data/llm_by_model.csv produced by tracker.py and renders replicas of
Figure 5 (top models by volume) and Figure 6 (price vs volume scatter), plus
the volume-vs-spend divergence chart the original report does not include.

    python make_charts.py --data ./data --out ./charts
"""
import argparse, csv, io, math, os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# --- reference palette (dataviz skill, slots 1-2 unmodified) -----------------
SURFACE   = "#fcfcfb"
INK       = "#0b0b0b"
INK_2     = "#52514e"
MUTED     = "#898781"
GRID      = "#e1e0d9"
BASELINE  = "#c3c2b7"
S1        = "#2a78d6"   # blue   - volume
S2        = "#eb6834"   # orange - spend

plt.rcParams.update({
    "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"],
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "text.color": INK, "axes.labelcolor": INK_2,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.edgecolor": BASELINE, "axes.linewidth": 0.8,
    "font.size": 9,
})


def style(ax, ygrid=True, xgrid=False):
    ax.set_axisbelow(True)
    ax.grid(axis="y" if ygrid else "x", color=GRID, lw=0.8, zorder=0)
    if xgrid:
        ax.grid(axis="x", color=GRID, lw=0.8, zorder=0)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color(BASELINE)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(length=0, pad=4)


def title(fig, head, sub, source):
    fig.text(0.008, 0.975, head, size=13, weight="bold", color=INK, va="top")
    fig.text(0.008, 0.918, sub, size=9.5, color=INK_2, va="top")
    fig.text(0.008, 0.018, source, size=7.5, color=MUTED, va="bottom")


def short(slug, cap=26):
    s = slug.split("/")[-1]
    parts = s.split("-")
    if parts and parts[-1].isdigit() and len(parts[-1]) == 8:
        parts = parts[:-1]
    s = "-".join(parts)
    return s if len(s) <= cap else s[:cap - 1] + "…"


def load(path):
    rows = []
    with io.open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["total_tokens"] = float(r["total_tokens"])
                r["prompt_tokens"] = float(r["prompt_tokens"])
                r["completion_tokens"] = float(r["completion_tokens"])
            except ValueError:
                continue
            for k in ("est_spend_usd", "list_price_blended_usd_per_mn"):
                r[k] = float(r[k]) if r[k] not in ("", None) else None
            rows.append(r)
    # keep only the latest snapshot
    latest = max(r["run_date"] for r in rows)
    cur = [r for r in rows if r["run_date"] == latest]
    # same-day re-runs append a second snapshot; keep one row per model
    seen, out = set(), []
    for r in cur:
        k = (r["model"], r.get("variant", ""))
        if k in seen:
            continue
        seen.add(k); out.append(r)
    return out, latest


# ----------------------------------------------------------------- figure 5
def fig5(rows, asof, out):
    top = sorted(rows, key=lambda r: -r["total_tokens"])[:20]
    fig, ax = plt.subplots(figsize=(9.6, 5.2))
    fig.subplots_adjust(left=0.062, right=0.99, top=0.795, bottom=0.365)
    vals = [r["total_tokens"] / 1e12 for r in top]
    ax.bar(range(len(top)), vals, width=0.72, color=S1, zorder=3)
    style(ax)
    ax.set_xticks(range(len(top)))
    ax.set_xticklabels([short(r["model"], 17) + (" :free" if r["variant"] == "free" else "")
                        for r in top], rotation=55, ha="right", size=7.6, color=INK_2)
    ax.set_ylabel("Weekly tokens (trillion)", size=9, color=INK_2)
    ax.set_xlim(-0.7, len(top) - 0.3)
    # selective direct labels: top 3 only
    for i, v in enumerate(vals[:3]):
        ax.text(i, v + max(vals) * 0.02, "%.1fT" % v, ha="center",
                size=8, weight="bold", color=INK)
    title(fig, "Top 20 models by weekly token volume",
          "Rebuilt from OpenRouter rankings + price list  ·  week ending %s  ·  replica of JPM Figure 5" % asof,
          "Source: OpenRouter (openrouter.ai/rankings), as of %s. Own calculations." % asof)
    p = os.path.join(out, "fig05_top20_volume.png")
    fig.savefig(p, dpi=170); plt.close(fig); return p


# ----------------------------------------------------------------- figure 6
def fig6(rows, asof, out):
    pts = [r for r in rows
           if r["list_price_blended_usd_per_mn"] and r["total_tokens"] > 0
           and r["variant"] != "free"]
    fig, ax = plt.subplots(figsize=(9.6, 5.4))
    fig.subplots_adjust(left=0.085, right=0.985, top=0.80, bottom=0.115)
    x = [r["total_tokens"] / 1e9 for r in pts]
    y = [r["list_price_blended_usd_per_mn"] for r in pts]
    ax.scatter(x, y, s=34, color=S1, alpha=0.72, linewidths=0.8,
               edgecolors=SURFACE, zorder=3)
    ax.set_xscale("log"); ax.set_yscale("log")
    style(ax, ygrid=True, xgrid=True)
    ax.set_xlabel("Weekly token usage (billions, log)", size=9, color=INK_2)
    ax.set_ylabel("Realized blended price ($ per Mn tokens, log)", size=9, color=INK_2)
    ax.yaxis.set_major_formatter(FuncFormatter(
        lambda v, p: ("$%g" % v) if v >= 1 else ("$%.2f" % v)))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, p: "%g" % v))

    # label the frontier: most expensive, cheapest, highest volume, and big-spend
    def lab(r, dx, dy, ha="left"):
        ax.annotate(short(r["model"]),
                    (r["total_tokens"] / 1e9, r["list_price_blended_usd_per_mn"]),
                    textcoords="offset points", xytext=(dx, dy), ha=ha, size=7.8,
                    color=INK, zorder=5,
                    arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.7,
                                    shrinkA=0, shrinkB=3))
    byp = sorted(pts, key=lambda r: -r["list_price_blended_usd_per_mn"])
    byv = sorted(pts, key=lambda r: -r["total_tokens"])
    bys = sorted([r for r in pts if r["est_spend_usd"]], key=lambda r: -r["est_spend_usd"])
    for r, d in [(byp[0], (10, 6)), (byp[-1], (10, -12)), (byv[0], (-12, 12)),
                 (byv[1], (-10, -16)), (bys[0], (12, 8))]:
        lab(r, *d)
    n = len(pts)
    ax.text(0.985, 0.045, "%d priced models" % n, transform=ax.transAxes,
            ha="right", size=8, color=MUTED)
    title(fig, "Token price vs. weekly volume",
          "Every priced model on OpenRouter, log-log  ·  week ending %s  ·  replica of JPM Figure 6" % asof,
          "Source: OpenRouter (openrouter.ai/rankings), as of %s. Price from OpenRouter model list; blend uses actual input/output mix." % asof)
    p = os.path.join(out, "fig06_price_vs_volume.png")
    fig.savefig(p, dpi=170); plt.close(fig); return p


# ------------------------------------------------- volume vs spend divergence
def fig_divergence(rows, asof, out):
    paid = [r for r in rows if r["est_spend_usd"] is not None]
    T = sum(r["total_tokens"] for r in rows)
    S = sum(r["est_spend_usd"] for r in paid)
    agg = {}
    for r in paid:
        a = agg.setdefault(r["author"], [0.0, 0.0])
        a[0] += r["total_tokens"]; a[1] += r["est_spend_usd"]
    for r in rows:
        if r["est_spend_usd"] is None:
            agg.setdefault(r["author"], [0.0, 0.0])[0] += r["total_tokens"]
    top = sorted(agg.items(), key=lambda kv: -(kv[1][1]))[:12]

    fig, ax = plt.subplots(figsize=(9.6, 5.0))
    fig.subplots_adjust(left=0.105, right=0.99, top=0.795, bottom=0.235)
    idx = range(len(top))
    w = 0.38
    v = [100 * kv[1][0] / T for kv in top]
    s = [100 * kv[1][1] / S for kv in top]
    ax.bar([i - w / 2 - 0.012 for i in idx], v, width=w, color=S1,
           label="Share of token volume", zorder=3)
    ax.bar([i + w / 2 + 0.012 for i in idx], s, width=w, color=S2,
           label="Share of estimated spend", zorder=3)
    style(ax)
    ax.set_xticks(list(idx))
    ax.set_xticklabels([kv[0] for kv in top], rotation=40, ha="right",
                       size=8.2, color=INK_2)
    ax.set_ylabel("% of OpenRouter total", size=9, color=INK_2)
    ax.set_xlim(-0.65, len(top) - 0.35)
    leg = ax.legend(frameon=False, loc="upper right", fontsize=8.6,
                    handlelength=1.1, handleheight=0.9)
    for t in leg.get_texts():
        t.set_color(INK_2)
    title(fig, "Volume and revenue have decoupled",
          "Chinese open-weight labs supply the tokens; Anthropic and OpenAI collect the spend  ·  week ending %s" % asof,
          "Source: OpenRouter (openrouter.ai/rankings), as of %s. Spend = own estimate at list price, actual input/output mix." % asof)
    p = os.path.join(out, "fig_extra_volume_vs_spend.png")
    fig.savefig(p, dpi=170); plt.close(fig); return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./data")
    ap.add_argument("--out", default="./charts")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rows, asof = load(os.path.join(a.data, "llm_by_model.csv"))
    print("loaded %d model rows, snapshot %s" % (len(rows), asof))
    for fn in (fig5, fig6, fig_divergence):
        print("  ->", fn(rows, asof, a.out))


if __name__ == "__main__":
    main()
