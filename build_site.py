# -*- coding: utf-8 -*-
"""Render the dashboard to a single self-contained docs/index.html.

No CDN, no npm, no external requests: charts are inline SVG generated here,
interactivity is a few dozen lines of vanilla JS. The page loads instantly,
works offline, and cannot break because a third-party host is unreachable.

    python build_site.py --data ./data --state ./state --out ./docs
"""
import argparse, csv, datetime as dt, glob, io, json, os
from html import escape

# ---- reference palette (dataviz skill), light | dark ----------------------
PAL = {
    "surface":  ("#fcfcfb", "#1a1a19"),
    "plane":    ("#f9f9f7", "#0d0d0d"),
    "ink":      ("#0b0b0b", "#ffffff"),
    "ink2":     ("#52514e", "#c3c2b7"),
    "muted":    ("#898781", "#898781"),
    "grid":     ("#e1e0d9", "#2c2c2a"),
    "baseline": ("#c3c2b7", "#383835"),
    "s1":       ("#2a78d6", "#3987e5"),
    "s2":       ("#eb6834", "#d95926"),
    "s3":       ("#1baf7a", "#199e70"),
    "s4":       ("#eda100", "#c98500"),
    "s5":       ("#e87ba4", "#d55181"),
    "s6":       ("#008300", "#008300"),
    "s7":       ("#4a3aa7", "#9085e9"),
    "s8":       ("#e34948", "#e66767"),
    # ordinal ramp for break-even magnitude: darker = needs more concurrency =
    # worse. One hue, light->dark, per the sequential rule; three categorical
    # hues here made blue (the neutral-reading default) land on the worst cases.
    "seq1":     ("#86b6ef", "#1c5cab"),
    "seq2":     ("#2a78d6", "#3987e5"),
    "seq3":     ("#104281", "#9ec5f4"),
    "good":     ("#006300", "#0ca30c"),
    "bad":      ("#d03b3b", "#e66767"),
}

# TrendForce is a commercial data vendor. On a public page we publish the
# indexed series and percentage moves rather than republishing their absolute
# quotes. Flip with --memory-absolute if you decide otherwise.
MEMORY_ABSOLUTE = False


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


def dedupe(rows, key):
    out = {}
    for r in rows:
        out[key(r)] = r
    return list(out.values())


def dparse(s):
    return dt.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))


# ------------------------------------------------------------------ format
def fmt_tokens(v):
    if v is None:
        return "—"
    if v >= 1e12:
        return "%.2fT" % (v / 1e12)
    if v >= 1e9:
        return "%.1fB" % (v / 1e9)
    return "%.0f" % v


def fmt_usd(v):
    if v is None:
        return "—"
    if abs(v) >= 1e6:
        return "$%.1fM" % (v / 1e6)
    if abs(v) >= 1e3:
        return "$%.1fk" % (v / 1e3)
    return "$%.2f" % v


def fmt_price(v):
    return "—" if v is None else "$%.3f" % v


def fmt_pct(v):
    return "—" if v is None else "%.1f%%" % v


def fmt_delta(v):
    """Direction is shown by the glyph, not by colour. Green/red would assert
    that "up is good", which is meaningless for VWAP, price levels and share
    metrics — and status colours are reserved for actual status."""
    if v is None:
        return '<span class="d-na">—</span>'
    arrow = "▲" if v > 0 else ("▼" if v < 0 else "•")
    return '<span class="d-chg">%s %.2f%%</span>' % (arrow, abs(v))


# --------------------------------------------------------------------- SVG
def _nice(lo, hi):
    """Round an axis range outward to readable bounds."""
    if lo == hi:
        lo, hi = (lo * 0.95, hi * 1.05) if lo else (0.0, 1.0)
    span = hi - lo
    step = 10 ** (len("%d" % int(abs(span))) - 1) if span >= 1 else 10 ** -3
    while span / step > 8:
        step *= 2
    while span / step < 3:
        step /= 2.0
    import math
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def line_chart(cid, series, ylabel, yfmt=lambda v: "%.2f" % v,
               height=230, gap_days=2, zero_base=False, tipfmt=None,
               end_labels=False, xfmt=None, annot=None):
    """series: [{'name':..., 'color_role':'s1', 'points':[(date_str, value)]}]

    A gap longer than gap_days starts a new subpath: a permanently missing day
    is drawn as a break, never as a straight line across the hole. Aggregated
    series must widen gap_days past their own spacing (weekly points sit 7 days
    apart, so the default 2 would break every single segment).

    xfmt formats the x tick label; points still have to be YYYY-MM-DD because
    the gap test parses them as dates. annot maps a date to an extra string
    appended to that point's tooltip — for a rolled-up series it is where the
    bucket's own total and day count go, which a value-only formatter cannot
    carry."""
    live = [s for s in series if len(s["points"]) >= 1]
    if not live:
        return '<div class="empty">暂无数据</div>'
    allpts = [p for s in live for p in s["points"]]
    n_uniq = len({p[0] for p in allpts})
    if n_uniq < 2:
        return ('<div class="empty">仅 %d 个观测点，需累计至少 2 天才能画趋势线。'
                '当前值见上方 KPI。</div>' % n_uniq)

    W, H = 860, height
    ml, mr, mt, mb = 62, (96 if end_labels else 16), 28, 34
    pw, ph = W - ml - mr, H - mt - mb
    xs = sorted({p[0] for p in allpts})
    xi = {d: i for i, d in enumerate(xs)}
    vals = [p[1] for p in allpts if p[1] is not None]
    lo, hi = (0.0 if zero_base else min(vals)), max(vals)
    lo, hi, step = _nice(lo, hi)

    def px(d):
        return ml + (pw * xi[d] / max(1, len(xs) - 1))

    def py(v):
        return mt + ph - ph * (v - lo) / (hi - lo or 1)

    tf = tipfmt or yfmt
    multiyear = xs[0][:4] != xs[-1][:4]
    # 571 points x 2 series = 1100+ markers, which buries the line. Past this
    # threshold draw the line only and hand hover to a crosshair overlay.
    dense = len(xs) > 60
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (W, H)]
    # gridlines + y ticks
    t = lo
    while t <= hi + step / 2:
        y = py(t)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" class="tick ta-end">%s</text>' % (ml - 8, y + 3.5, escape(yfmt(t))))
        t += step
    # x ticks (at most 8). The last date is always labelled, but only if it
    # clears the previous label — otherwise the forced final tick collides with
    # the regular one a couple of points behind it.
    everyx = max(1, len(xs) // 8)
    last_x = None
    for i, d in enumerate(xs):
        is_last = i == len(xs) - 1
        if i % everyx and not is_last:
            continue
        x = px(d)
        if last_x is not None and x - last_x < (46 if xfmt else (58 if multiyear else 34)):
            if not is_last:
                continue
            out.pop()          # drop the previous label in favour of the last
        out.append('<text x="%.1f" y="%d" class="tick ta-mid">%s</text>'
                   % (x, H - 12, escape(xfmt(d) if xfmt else (d if multiyear else d[5:]))))
        last_x = x
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="axis"/>' % (ml, mt + ph, W - mr, mt + ph))

    for s in live:
        col = "var(--%s)" % s["color_role"]
        pts = sorted([p for p in s["points"] if p[1] is not None], key=lambda p: p[0])
        d, prev = [], None
        for ds, v in pts:
            cmd = "M" if (prev is None or (dparse(ds) - dparse(prev)).days > gap_days) else "L"
            d.append("%s%.1f %.1f" % (cmd, px(ds), py(v)))
            prev = ds
        out.append('<path d="%s" fill="none" stroke="%s" stroke-width="2" '
                   'stroke-linejoin="round" stroke-linecap="round"/>' % (" ".join(d), col))
        if not dense:
            for ds, v in pts:
                extra = ("  ·  " + annot[ds]) if (annot and annot.get(ds)) else ""
                out.append('<circle cx="%.1f" cy="%.1f" r="3.6" fill="%s" stroke="var(--surface)" '
                           'stroke-width="1.5" class="pt" data-tip="%s"/>'
                           % (px(ds), py(v), col,
                              escape("%s · %s: %s%s" % (ds, s["name"], tf(v), extra))))
    if dense:
        import json as _j
        payload = {"x": xs, "ml": ml, "pw": pw, "mt": mt, "ph": ph,
                   "s": [{"n": sr["name"], "c": sr["color_role"],
                          "v": [dict(sr["points"]).get(d) for d in xs],
                          "f": [tf(dict(sr["points"])[d]) if dict(sr["points"]).get(d) is not None
                                else None for d in xs]} for sr in live]}
        if annot:
            # only when there is something to say — otherwise this is one empty
            # string per x, and a 576-point daily series pays for all of them
            payload["a"] = [annot.get(d, "") for d in xs]
        out.append('<line class="cross" x1="0" y1="%d" x2="0" y2="%d" '
                   'stroke="var(--ink)" stroke-width="1" opacity="0"/>' % (mt, mt + ph))
        out.append('<rect class="hit" x="%d" y="%d" width="%d" height="%d" fill="transparent" '
                   "data-series='%s'/>" % (ml, mt, pw, ph,
                                           escape(_j.dumps(payload, ensure_ascii=False))))
    if end_labels:
        # nudge overlapping labels apart so eight lines ending close together
        # stay readable
        ends = []
        for sr in live:
            pts = [p for p in sr["points"] if p[1] is not None]
            if pts:
                ends.append([py(pts[-1][1]), sr["name"], sr["color_role"]])
        ends.sort()
        for i in range(1, len(ends)):
            if ends[i][0] - ends[i - 1][0] < 10:
                ends[i][0] = ends[i - 1][0] + 10
        # a cluster near the bottom of the scale gets pushed past the axis and
        # lands on the date ticks, so pull the whole stack back inside the plot
        if ends:
            over = ends[-1][0] - (mt + ph)
            if over > 0:
                shift = min(over, ends[0][0] - mt)
                for e in ends:
                    e[0] -= shift
        for y, name, role in ends:
            out.append('<text x="%d" y="%.1f" class="endlab" fill="var(--%s)">%s</text>'
                       % (W - mr + 6, y + 3, role, escape(name)))
    out.append('<text x="6" y="12" class="axlabel">%s</text>' % escape(ylabel))
    out.append("</svg>")
    return "".join(out)


def bar_chart(cid, labels, values, ylabel, yfmt=lambda v: "%.0f" % v,
              color_role="s1", height=230):
    if not values:
        return '<div class="empty">暂无数据</div>'
    W, H = 860, height
    ml, mr, mt, mb = 62, 16, 28, 44
    pw, ph = W - ml - mr, H - mt - mb
    hi = max(values) or 1.0
    lo, hi, step = _nice(0.0, hi)
    slot = pw / max(1, len(values))
    # cap the bar width so a 2-point series does not render as two huge slabs
    barw = max(2.0, min(slot - 2, 46.0))

    def py(v):
        return mt + ph - ph * (v - lo) / (hi - lo or 1)

    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (W, H)]
    t = lo
    while t <= hi + step / 2:
        y = py(t)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" class="tick ta-end">%s</text>' % (ml - 8, y + 3.5, escape(yfmt(t))))
        t += step
    for i, (lb, v) in enumerate(zip(labels, values)):
        cx = ml + i * slot + slot / 2.0
        y = py(v)
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" '
                   'fill="var(--%s)" class="pt" data-tip="%s"/>'
                   % (cx - barw / 2, y, barw, max(0.0, mt + ph - y), color_role,
                      escape("%s: %s" % (lb, yfmt(v)))))
        if i % max(1, len(values) // 12) == 0 or len(values) <= 14:
            out.append('<text x="%.1f" y="%d" class="tick ta-end rot" transform="rotate(-42 %.1f %d)">%s</text>'
                       % (cx, H - 12, cx, H - 12, escape(lb[:22])))
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="axis"/>' % (ml, mt + ph, W - mr, mt + ph))
    out.append('<text x="6" y="12" class="axlabel">%s</text>' % escape(ylabel))
    out.append("</svg>")
    return "".join(out)


def dbar_chart(cid, rows, ylabel, yfmt=lambda v: "%+.0f%%" % v, height=250,
               pos_role="s1", neg_role="s2", label_idx=()):
    """Diverging bars around a zero baseline. rows: [(label, value, tip)].

    `bar_chart` anchors its scale at zero via _nice(0.0, hi) and clamps bar
    height to >= 0, so a negative value there collapses to nothing. Growth rates
    go both ways, hence a separate primitive: the zero line is the visual
    baseline, bars grow up or down from it, and sign gets a second encoding in
    hue so it does not rest on bar direction alone.

    `label_idx` are the row indices that get a direct value label — per the
    selective-labelling rule, not every bar carries a number."""
    if not rows:
        return '<div class="empty">暂无数据</div>'
    W, H = 860, height
    ml, mr, mt, mb = 62, 16, 30, 50
    pw, ph = W - ml - mr, H - mt - mb
    vals = [r[1] for r in rows]
    lo, hi, step = _nice(min(min(vals), 0.0), max(max(vals), 0.0))

    slot = pw / max(1, len(rows))
    barw = max(2.0, min(slot - 3, 40.0))

    def py(v):
        return mt + ph - ph * (v - lo) / (hi - lo or 1)

    y0 = py(0.0)
    # label every bar only while they still fit; a daily series is 575 bars and
    # would otherwise emit 575 overlapping rotated labels
    everyx = 1 if len(rows) <= 20 else max(1, len(rows) // 12)
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (W, H)]
    t = lo
    while t <= hi + step / 2:
        y = py(t)
        if abs(t) > step / 2:      # zero gets the emphasised line below instead
            out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>'
                       % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" class="tick ta-end">%s</text>'
                   % (ml - 8, y + 3.5, escape(yfmt(t))))
        t += step

    for i, row in enumerate(rows):
        lb, v, tip = row[0], row[1], row[2]
        cx = ml + i * slot + slot / 2.0
        role = pos_role if v >= 0 else neg_role
        top = min(py(v), y0)
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" '
                   'fill="var(--%s)" class="pt" data-tip="%s"/>'
                   % (cx - barw / 2, top, barw, max(1.0, abs(py(v) - y0)), role,
                      escape(tip)))
        if i % everyx == 0 or i == len(rows) - 1:
            out.append('<text x="%.1f" y="%d" class="tick ta-end rot" transform="rotate(-42 %.1f %d)">%s</text>'
                       % (cx, H - 14, cx, H - 14, escape(lb)))
        if i in label_idx:
            # sit the label outside the bar's free end, and keep it inside the
            # plot — the last bar sits flush against the right margin, so a
            # centred label there hangs half of itself off the chart
            ly = (py(v) - 6) if v >= 0 else (py(v) + 13)
            lx = min(max(cx, ml + 14), W - mr - 14)
            out.append('<text x="%.1f" y="%.1f" class="tick ta-mid">%s</text>'
                       % (lx, ly, escape(yfmt(v))))
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="axis"/>'
               % (ml, y0, W - mr, y0))
    out.append('<text x="6" y="12" class="axlabel">%s</text>' % escape(ylabel))
    out.append("</svg>")
    return "".join(out)


def hbar_chart(cid, rows, vfmt, color_role="s1", height=None, width=430):
    """rows: [(label, value, tip)] already sorted desc.

    `width` is the viewBox width. The SVG scales to its container, so a
    narrow viewBox inside a full-width panel magnifies all its text; pass
    860 for full-width panels and keep the default in two-column layouts."""
    if not rows:
        return '<div class="empty">暂无数据</div>'
    n = len(rows)
    rowh = 21
    W = width
    H = height or (n * rowh + 16)
    ml, mr = int(W * 0.39), int(W * 0.126)
    pw = W - ml - mr
    hi = max(r[1] for r in rows) or 1.0
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (W, H)]
    for i, row in enumerate(rows):
        lb, v, tip = row[0], row[1], row[2]
        vlabel = row[3] if len(row) > 3 else vfmt(v)
        y = 8 + i * rowh
        w = pw * (v / hi)
        out.append('<text x="%d" y="%.1f" class="tick ta-end">%s</text>' % (ml - 8, y + 11, escape(lb[:26])))
        out.append('<rect x="%d" y="%.1f" width="%.1f" height="%d" rx="3" fill="var(--%s)" '
                   'class="pt" data-tip="%s"/>' % (ml, y + 2, max(1.0, w), rowh - 6, color_role, escape(tip)))
        out.append('<text x="%.1f" y="%.1f" class="tick">%s</text>' % (ml + w + 6, y + 11, escape(vlabel)))
    out.append("</svg>")
    return "".join(out)


def scatter_chart(cid, pts, xlabel, ylabel, height=380):
    """pts: [(x, y, label)] on log-log axes."""
    import math
    pts = [(x, y, l) for x, y, l in pts if x and y and x > 0 and y > 0]
    if len(pts) < 2:
        return '<div class="empty">暂无数据</div>'
    W, H = 860, height
    ml, mr, mt, mb = 66, 100, 28, 42
    pw, ph = W - ml - mr, H - mt - mb
    lxs = [math.log10(p[0]) for p in pts]
    lys = [math.log10(p[1]) for p in pts]
    x0, x1 = math.floor(min(lxs)), math.ceil(max(lxs))
    y0, y1 = math.floor(min(lys)), math.ceil(max(lys))

    def px(x):
        return ml + pw * (math.log10(x) - x0) / ((x1 - x0) or 1)

    def py(y):
        return mt + ph - ph * (math.log10(y) - y0) / ((y1 - y0) or 1)

    def dec(e):
        return ("%g" % (10 ** e)) if e >= 0 else ("%.*f" % (-e, 10 ** e))

    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (W, H)]
    for e in range(int(y0), int(y1) + 1):
        y = py(10 ** e)
        out.append('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" class="grid"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" class="tick ta-end">$%s</text>' % (ml - 8, y + 3.5, dec(e)))
    for e in range(int(x0), int(x1) + 1):
        x = px(10 ** e)
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" class="grid"/>' % (x, mt, x, mt + ph))
        out.append('<text x="%.1f" y="%d" class="tick ta-mid">%s</text>' % (x, H - 22, dec(e)))
    for x, y, lb in pts:
        out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="var(--s1)" fill-opacity="0.72" '
                   'stroke="var(--surface)" stroke-width="0.8" class="pt" data-tip="%s"/>'
                   % (px(x), py(y), escape("%s · %.3gB tokens · $%.4g/Mn" % (lb, x, y))))
    # direct-label the four corners of the frontier only. The max-x point sits
    # at the right edge, so its label goes outward into the right margin rather
    # than leftward across the cloud.
    by_y = sorted(pts, key=lambda p: p[1])
    by_x = sorted(pts, key=lambda p: p[0])
    for p, dx, dy in ((by_y[-1], 8, -6), (by_y[0], 8, 12), (by_x[-1], 8, -8), (by_x[0], 8, -6)):
        out.append('<text x="%.1f" y="%.1f" class="ptlabel" text-anchor="start">%s</text>'
                   % (px(p[0]) + dx, py(p[1]) + dy, escape(p[2][:22])))
    out.append('<text x="6" y="12" class="axlabel">%s</text>' % escape(ylabel))
    out.append('<text x="%d" y="%d" class="axlabel ta-mid">%s</text>' % (ml + pw // 2, H - 6, escape(xlabel)))
    out.append("</svg>")
    return "".join(out)


def range_bar(cid, rows, vfmt, color_role="s1"):
    """rows: [(label, lo, mid, hi, tip)] — a dot for the median on a min-max
    whisker. GPU prices vary 3-4x across clouds, so a single number would hide
    the dispersion that matters when judging whether a quote is competitive."""
    if not rows:
        return '<div class="empty">暂无数据</div>'
    n = len(rows)
    rowh, W, ml, mr = 26, 430, 86, 62
    H = n * rowh + 18
    pw = W - ml - mr
    hi_all = max(r[3] for r in rows) or 1.0
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (W, H)]
    for i, (lb, lo, mid, hi, tip) in enumerate(rows):
        y = 12 + i * rowh
        x0, x1 = ml + pw * (lo / hi_all), ml + pw * (hi / hi_all)
        xm = ml + pw * (mid / hi_all)
        out.append('<text x="%d" y="%.1f" class="tick ta-end">%s</text>' % (ml - 8, y + 4, escape(lb)))
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--%s)" '
                   'stroke-width="2" stroke-opacity="0.30" stroke-linecap="round"/>'
                   % (x0, y, x1, y, color_role))
        out.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="var(--%s)" '
                   'stroke="var(--surface)" stroke-width="1.5" class="pt" data-tip="%s"/>'
                   % (xm, y, color_role, escape(tip)))
        out.append('<text x="%.1f" y="%.1f" class="tick">%s</text>' % (W - mr + 6, y + 4, escape(vfmt(mid))))
    out.append("</svg>")
    return "".join(out)


def bridge_bar(cid, rows, band=(20.0, 200.0)):
    """rows: [(label, break_even_streams, tip)] on a log x-axis.

    The shaded band is the concurrency real serving stacks actually run at.
    A model whose bar ends left of the band earns its GPU back on a handful of
    streams; one ending inside or right of it is running thin."""
    import math
    rows = [r for r in rows if r[1] and r[1] > 0]
    if not rows:
        return '<div class="empty">暂无数据</div>'
    n = len(rows)
    rowh, W, ml, mr = 22, 860, 210, 66
    H = n * rowh + 40
    pw = W - ml - mr
    lo = min(min(r[1] for r in rows), band[0]) * 0.6
    hi = max(max(r[1] for r in rows), band[1]) * 1.3
    l0, l1 = math.log10(lo), math.log10(hi)

    def px(v):
        return ml + pw * (math.log10(v) - l0) / ((l1 - l0) or 1)

    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (W, H)]
    out.append('<rect x="%.1f" y="6" width="%.1f" height="%d" fill="var(--s3)" '
               'fill-opacity="0.10"/>' % (px(band[0]), px(band[1]) - px(band[0]), n * rowh + 4))
    out.append('<text x="%.1f" y="%d" class="tick ta-mid">真实服务并发区间 %g–%g 路</text>'
               % ((px(band[0]) + px(band[1])) / 2, H - 10, band[0], band[1]))
    e = int(math.floor(l0))
    while e <= math.ceil(l1):
        v = 10 ** e
        if lo <= v <= hi:
            out.append('<line x1="%.1f" y1="6" x2="%.1f" y2="%d" class="grid"/>'
                       % (px(v), px(v), n * rowh + 10))
            out.append('<text x="%.1f" y="%d" class="tick ta-mid">%g</text>' % (px(v), H - 24, v))
        e += 1
    for i, (lb, v, tip) in enumerate(rows):
        y = 10 + i * rowh
        # ordinal, not categorical: below the band / inside it / above it
        step = "seq1" if v < band[0] else ("seq2" if v < band[1] else "seq3")
        out.append('<text x="%d" y="%.1f" class="tick ta-end">%s</text>' % (ml - 8, y + 11, escape(lb[:30])))
        out.append('<rect x="%d" y="%.1f" width="%.1f" height="%d" rx="3" fill="var(--%s)" '
                   'class="pt" data-tip="%s"/>'
                   % (ml, y + 3, max(2.0, px(v) - ml), rowh - 8, step, escape(tip)))
        out.append('<text x="%.1f" y="%.1f" class="tick">%.1f</text>' % (px(v) + 6, y + 11, v))
    out.append("</svg>")
    return "".join(out)


def small_multiples(cid, panels, yfmt=lambda v: "%.0f%%" % v, cols=4,
                    color_role="s1", shared_scale=True):
    """One mini chart per entity — the sanctioned way to show more series than a
    categorical palette can distinguish. 19 vendors would need 19 hues; here each
    panel carries one series, so colour never has to separate anything.

    panels: [{'name':.., 'points':[(x,y)], 'head':.., 'sub':..}]
    A shared y-scale keeps the panels comparable; per-panel scales would make a
    0.5% vendor look like a 20% one.
    """
    panels = [p for p in panels if len(p.get("points") or []) >= 2]
    if not panels:
        return '<div class="empty">暂无数据</div>'
    hi = max((v for p in panels for _, v in p["points"] if v is not None), default=1.0) or 1.0
    W, H = 210, 74
    ml, mr, mt, mb = 4, 4, 22, 12
    pw, ph = W - ml - mr, H - mt - mb
    out = ['<div class="smgrid" style="grid-template-columns:repeat(%d,1fr)">' % cols]
    for p in panels:
        pts = [(x, v) for x, v in p["points"] if v is not None]
        n = len(pts)
        top = hi if shared_scale else (max(v for _, v in pts) or 1.0)
        def px(i):
            return ml + pw * i / max(1, n - 1)
        def py(v):
            return mt + ph - ph * (v / top)
        d = " ".join("%s%.1f %.1f" % ("M" if i == 0 else "L", px(i), py(v))
                     for i, (_, v) in enumerate(pts))
        area = d + " L%.1f %.1f L%.1f %.1f Z" % (px(n - 1), mt + ph, px(0), mt + ph)
        tip = "%s · %s → %s" % (p["name"], yfmt(pts[0][1]), yfmt(pts[-1][1]))
        out.append(
            '<div class="sm" data-tip="%s">'
            '<svg viewBox="0 0 %d %d" class="chart" role="img">'
            '<text x="%d" y="11" class="smname">%s</text>'
            '<text x="%d" y="11" class="smval ta-end">%s</text>'
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>'
            '<path d="%s" fill="var(--%s)" fill-opacity="0.14" stroke="none"/>'
            '<path d="%s" fill="none" stroke="var(--%s)" stroke-width="1.6" '
            'stroke-linejoin="round"/>'
            '<circle cx="%.1f" cy="%.1f" r="2.4" fill="var(--%s)"/>'
            '</svg></div>'
            % (escape(tip), W, H,
               ml, escape(p.get("head") or p["name"]),
               W - mr, escape(p.get("sub") or ""),
               ml, mt + ph, W - mr, mt + ph,
               area, color_role, d, color_role,
               px(n - 1), py(pts[-1][1]), color_role))
    out.append("</div>")
    return "".join(out)


def table_of(headers, rows):
    h = "".join("<th>%s</th>" % escape(x) for x in headers)
    b = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % escape(str(c)) for c in r) for r in rows)
    return '<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (h, b)


def sechead(layer, title, sub):
    return ('<div class="sechead"><span class="lchip">%s</span>'
            '<h3>%s</h3><span class="ssub">%s</span></div>'
            % (escape(layer), escape(title), escape(sub)))


# Panels are grouped into blocks by their place in the transmission chain:
# demand -> price -> cost -> profitability -> valuation. A marker is pushed into
# the html list and the assembler splits on it, which keeps the existing
# H.append(...) call sites untouched.
BLK = "\x00BLK\x00"


def blk(bid, label, title, sub="", signal=""):
    """`signal` is the transmission logic — what a move in this layer means and
    which link it propagates to next. The spec blocks say how each number is
    computed; without this the reader still does not know why to care."""
    return BLK + "\x00".join([bid, label, title, sub, signal])


def assemble(items):
    """Split the flat html list on block markers and wrap each group."""
    blocks, cur = [], None
    for it in items:
        if it.startswith(BLK):
            # strip the sentinel first: BLK itself contains a NUL, so splitting
            # the whole string would shift every field by one
            parts = it[len(BLK):].split("\x00")
            parts += [""] * (5 - len(parts))
            bid, label, title, sub, signal = parts[:5]
            cur = {"id": bid, "label": label, "title": title, "sub": sub,
                   "signal": signal, "html": []}
            blocks.append(cur)
        elif cur is None:
            blocks.append({"id": "", "label": "", "title": "", "sub": "",
                           "signal": "", "html": [it], "bare": True})
            cur = blocks[-1]
        else:
            cur["html"].append(it)

    nav = "".join(
        '<a href="#%s" data-nav="%s">%s</a>' % (b["id"], b["id"], escape(b["label"]))
        for b in blocks if b.get("id") and not b.get("bare"))
    body = []
    for b in blocks:
        if b.get("bare"):
            body.append("".join(b["html"]))
            continue
        body.append(
            '<section class="block" id="%s" data-block="%s">'
            '<button class="blkhead" type="button" aria-expanded="true">'
            '<span class="blabel">%s</span><span class="btitle">%s</span>'
            '<span class="bsub">%s</span><span class="bchev">▾</span></button>'
            '%s<div class="blkbody">%s</div></section>'
            % (b["id"], b["id"], escape(b["label"]), escape(b["title"]),
               escape(b["sub"]),
               ('<p class="bsignal"><b>信号逻辑</b>%s</p>' % escape(b["signal"]))
               if b.get("signal") else "",
               "".join(b["html"])))
    return nav, "".join(body)


def periodise(daily, keys, freq, min_days):
    """Roll a daily series up to weekly/monthly buckets.

    Returns [(bucket_date, {key: mean_daily}, {key: total}, n_days)] sorted by
    date, dropping buckets thinner than min_days.

    Every frequency reports MEAN DAILY volume, not the bucket's sum, and the
    y axis unit is therefore identical across all three views — switching
    frequency is pure smoothing, the level never jumps 7x or 30x. That is not
    just cosmetic; a sum-based rollup carries three artefacts this avoids:

      * calendar length — Feb vs Mar is +10.7% on day count alone
      * partial edge buckets — the current week/month is always mid-flight, so
        its sum dips and reads as a collapse
      * the two upstream holes (2025-06-15, 2025-07-15) would each shave ~1/7
        off their week

    The bucket total is still carried through for the tooltip, so "how much in
    total this week" stays one hover away.
    """
    buckets = {}
    for r in daily:
        d = dparse(sdate(r))
        if freq == "w":
            k = (d - dt.timedelta(days=d.weekday())).isoformat()   # ISO Monday
        elif freq == "m":
            k = "%04d-%02d-01" % (d.year, d.month)
        else:
            k = d.isoformat()
        b = buckets.setdefault(k, {"n": 0, "sum": {}})
        # count a day once, even if two keys land on it
        b["n"] += 1
        for key in keys:
            v = num(r.get(key))
            if v is not None:
                b["sum"][key] = b["sum"].get(key, 0.0) + v
    out = []
    for k in sorted(buckets):
        b = buckets[k]
        if b["n"] < min_days:
            continue
        means = {key: (b["sum"][key] / b["n"]) for key in b["sum"]}
        out.append((k, means, dict(b["sum"]), b["n"]))
    return out


def growth_of(buck, freq, key="total_tokens"):
    """Period-over-period growth (环比) from periodise() output.

    Emits a value only when the previous bucket is the IMMEDIATELY preceding
    period. Holes exist — 2025-06-15 is missing upstream, and thin edge buckets
    get dropped — and comparing across one silently produces a two-period change
    labelled as one period's 环比: the arithmetic is fine and the label is a lie.
    Skipping leaves a genuine gap instead.

    Returns [(bucket_date, pct, mean, n_days, prev_date)].
    """
    out = []
    for i in range(1, len(buck)):
        k, m, _t, n = buck[i]
        pk, pm = buck[i - 1][0], buck[i - 1][1]
        prev, cur = pm.get(key), m.get(key)
        if not prev or cur is None:
            continue
        d0, d1 = dparse(pk), dparse(k)
        if freq == "d":
            adj = (d1 - d0).days == 1
        elif freq == "w":
            adj = (d1 - d0).days == 7
        else:
            adj = (d1.year * 12 + d1.month) - (d0.year * 12 + d0.month) == 1
        if not adj:
            continue
        out.append((k, (cur / prev - 1.0) * 100.0, cur, n, pk))
    return out


def spec(scope=None, calc=None, src=None, warn=None):
    """The three questions every panel has to answer before its numbers mean
    anything: what is being counted, how it was computed, and where it came
    from. Rendered compactly and always visible — hiding it behind a toggle is
    how a chart ends up quoted without its caveats."""
    rows = []
    for label, val in (("口径", scope), ("算法", calc), ("来源", src)):
        if val:
            rows.append('<div class="sk">%s</div><div class="sv">%s</div>'
                        % (label, escape(val)))
    if not rows and not warn:
        return ""
    grid = ('<div class="spec">%s</div>' % "".join(rows)) if rows else ""
    w = ('<p class="specwarn"><b>注意</b>%s</p>' % escape(warn)) if warn else ""
    return grid + w


def panel(title, subtitle, svg, table_html=None, note=None, pid="", meta="",
          extra=""):
    """`note` is inserted as raw HTML, not escaped — it is where a panel puts
    per-frequency copy that has to toggle with the charts. `extra` goes on the
    section tag itself (a data-freqgroup, say)."""
    t = ('<details class="tbl"><summary>数据表</summary>%s</details>' % table_html) if table_html else ""
    n = ('<p class="note">%s</p>' % note) if note else ""
    return ('<section class="panel" id="%s"%s><h2>%s</h2><p class="sub">%s</p>%s%s%s%s</section>'
            % (pid, extra, escape(title), escape(subtitle), svg, meta, n, t))


# -------------------------------------------------------------------- data
def sdate(r):
    """Plot by the date the data describes (as_of), not the fetch date, so
    backfilled and live rows share one axis. Mirrors analyze.sdate."""
    return r.get("as_of") or r["run_date"]


def load_summary(datadir, window):
    rows = [r for r in read_csv(os.path.join(datadir, "llm_summary.csv"))
            if r.get("window") == window]
    if window == "day":
        hist = read_csv(os.path.join(datadir, "llm_history_volume.csv"))
        live = {sdate(r) for r in rows}
        rows = [h for h in hist if sdate(h) not in live] + rows
    rows = dedupe(rows, sdate)
    rows.sort(key=sdate)
    return rows


def load_models(datadir, window):
    rows = []
    for p in sorted(glob.glob(os.path.join(datadir, "llm_by_model_*.csv"))):
        rows.extend([r for r in read_csv(p) if r.get("window") == window])
    if not rows:
        return "", []
    latest = max(r["run_date"] for r in rows)
    cur = [r for r in rows if r["run_date"] == latest]
    # re-running the tracker the same day appends a second full snapshot;
    # without this the top-N charts and the scatter show each model twice
    cur = dedupe(cur, lambda r: (r["model"], r.get("variant", "")))
    return latest, cur


def build(datadir, statedir, outdir, window="week", memory_absolute=False):
    summary = load_summary(datadir, window)
    mdate, models = load_models(datadir, window)
    brief = {}
    bp = os.path.join(statedir, "brief.json")
    if os.path.exists(bp):
        brief = json.load(io.open(bp, encoding="utf-8"))
    views = []
    cp = os.path.join(statedir, "commentary.jsonl")
    if os.path.exists(cp):
        for line in io.open(cp, encoding="utf-8"):
            if line.strip():
                try:
                    views.append(json.loads(line))
                except ValueError:
                    pass
    view = views[-1] if views else None
    latest = summary[-1] if summary else {}
    metrics = (brief.get("llm") or {}).get("metrics") or {}
    meta = (brief.get("llm") or {}).get("meta") or {}

    def series_of(key, label, role):
        return {"name": label, "color_role": role,
                "points": [(sdate(r), num(r.get(key))) for r in summary
                           if num(r.get(key)) is not None]}

    def dl(key, field):
        return (metrics.get(key) or {}).get(field)

    H = []
    H.append(blk("overview", "概览", "今日变动与总体紧张度",
                 "观点速报 · 跨层 KPI · 扩散指数",
                 "整条链的方向汇总。六层里同时朝紧的越多，越可能是真实的需求驱动；只有价格或只有成本单独动，多半是某一环的局部扰动。"))
    # ---------------- commentary
    if view:
        stale = view.get("date") != dt.date.today().isoformat()
        # bullets carry their layer so the reader can see the chain order at a
        # glance; entries written before that change are plain strings
        bl = []
        for x in view.get("bullets", []):
            if isinstance(x, dict):
                lay, txt = x.get("layer") or "", x.get("text") or ""
            else:
                lay, txt = "", x
            bl.append('<li>%s%s</li>'
                      % (('<span class="blay">%s</span>' % escape(lay)) if lay else "",
                         escape(txt)))
        bl = "".join(bl)
        wt = "".join("<li>%s</li>" % escape(x) for x in view.get("watch", []))
        conf = escape(view.get("confidence", "?"))
        H.append(
            '<section class="panel view"><div class="viewhead">'
            '<h2>今日观点</h2>'
            '<span class="badge conf-%s">置信度 %s</span>%s</div>'
            '<p class="headline">%s</p><ul class="bullets">%s</ul>'
            '%s<p class="note">%s · %s 生成 · %s</p></section>'
            % (conf, conf,
               '<span class="badge warn">点评未更新（%s）</span>' % escape(view.get("date", "")) if stale else "",
               escape(view.get("headline", "")), bl,
               ('<p class="watchhead">留意</p><ul class="watch">%s</ul>' % wt) if wt else "",
               escape(view.get("model", "")), escape(view.get("generated_at", "")[:16]),
               escape(view.get("confidence_reason", ""))))
    else:
        H.append('<section class="panel view"><h2>今日观点</h2>'
                 '<p class="empty">尚未生成，运行 commentary.py。</p></section>')

    if meta.get("degraded"):
        H.append('<div class="banner">采集降级：%s</div>' % escape(meta.get("degrade_note", "")))
    if meta.get("gaps"):
        H.append('<div class="banner">存在 %d 个数据缺口日（图中显示为断点，不可回补）：%s</div>'
                 % (len(meta["gaps"]), escape(", ".join(meta["gaps"][:8]))))

    # ---------------- KPI row: one number from each layer, not six from one
    # Each tile carries a one-line definition (always visible) and a hover
    # tooltip with the full meaning plus its data source. Without them "需求"
    # and "5.0 路" mean nothing to anyone who did not build this.
    gm = brief.get("gpu_market") or {}
    br = brief.get("bridge") or {}
    hs_k = brief.get("hyperscaler") or {}
    h100 = gm.get("H100") or {}
    b200 = gm.get("B200") or {}
    nrep = (br.get("n_with_replica") or 0) if br else 0
    kpis = [
        ("需求", "周 Token 量", fmt_tokens(num(latest.get("total_tokens"))),
         dl("total_tokens", "dod"),
         "OpenRouter 全站近 7 日调用量",
         "OpenRouter 平台上 %d 个模型近 7 日被调用的 token 总数（输入+输出，含免费档）。"
         "衡量的是「有多少推理真的发生了」。"
         "注意这只是开发者与 agentic coding 切片，不含企业直连第一方 API 的流量，"
         "所以不是全市场总量。数据源：OpenRouter rankings 接口。"
         % (meta.get("models_tracked") or 0)),

        ("价格与收入", "Token 实际成交均价", fmt_price(num(latest.get("vwap_usd_per_mn"))) + "/Mn",
         dl("vwap_usd_per_mn", "dod"),
         "付费档每百万 token 的实际均价",
         "VWAP = 估算支出 ÷ 付费 token 数，即每 100 万 token 平均卖了多少钱。"
         "按每个模型真实的输入/输出配比加权（实际流量约 29:1 输入重），"
         "所以远低于「把各模型标价简单平均」得到的数。免费档不计入分子分母。"
         "数据源：OpenRouter 用量 × OpenRouter 官方价目表。"),

        ("价格与收入", "周支出", fmt_usd(num(latest.get("est_spend_usd"))),
         dl("est_spend_usd", "dod"),
         "按挂牌价估算，属上限",
         "近 7 日全站 token 花掉的钱，用「用量 × 官网挂牌价」估算。"
         "这是上限：真实成交有企业折扣、批量价，尤其是缓存命中只按 10% 计价，"
         "而输入 token 占 97% 且 agentic 工具反复复用 context。"
         "所以引用时说「不超过」，不要说「花了」。"),

        ("成本", "H100 租赁中位价", ("$%.2f/hr" % h100["value"]) if h100.get("value") else "—",
         h100.get("dod"),
         "%d 家 neocloud 跨云中位报价" % (h100.get("clouds") or 0),
         "租一张 H100 每小时多少钱，取 ~18 家中小型 GPU 云（neocloud）报价的中位数。"
         "同型号跨云价差常达 3-4 倍，所以用中位数而非单一报价。"
         "超大规模云挂牌价要贵约 6 倍（Azure 按需 $%s），在「成本」板块里并列显示。"
         "数据源：Shadeform 聚合接口。"
         % (("%.2f" % (hs_k.get("H100") or {}).get("value")) if (hs_k.get("H100") or {}).get("value") else "n/a")),

        ("成本", "B200 有货区域占比", ("%.0f%%" % b200["availability_pct"]) if b200.get("availability_pct") is not None else "—",
         b200.get("availability_delta"),
         "供给紧张度，越低越缺卡",
         "B200 在所有被监测的云区域中，当前标记为「有货可租」的比例。"
         "0% 意味着全线缺货。这是价格的领先指标——新卡通常先零可用、后涨价，"
         "所以它比租金更早反映供需转向。这个维度 JPM 的报告里没有。"
         "数据源：Shadeform 各区域实时可用状态。"),

        ("单位经济", "盈亏平衡并发",
         ("%.0f 路" % br["median_break_even_replica"])
         if br and br.get("median_break_even_replica") else
         (("%.1f 路" % br["median_break_even_streams"]) if br else "—"),
         None,
         "每副本需多少路并发才回本",
         "跑一个模型副本要租一批 GPU（大模型一个副本要 8-32 张卡）。"
         "这个数是：要同时服务多少路并发请求，token 收入才刚好够付这批卡的租金。"
         "越小越赚钱。真实推理服务通常跑几十到几百路并发，"
         "所以中位数落在 200 路附近意味着这些低价开源模型在挂牌价下贴近盈亏线。"
         "算法：GPU 时租 ÷（单流 tok/s × 3600 × 输出单价），再乘每副本卡数。"
         "%d 个开源模型可算，闭源模型不公开参数量因而无法估。" % nrep),
    ]
    H.append('<section class="kpis">%s</section>' % "".join(
        '<div class="kpi" data-tip="%s"><div class="klayer">%s</div><div class="kl">%s</div>'
        '<div class="kv">%s</div><div class="khint">%s</div>'
        '<div class="kd">较上次 %s</div></div>'
        % (escape(tip), escape(layer), escape(k), escape(v), escape(hint), fmt_delta(dd))
        for layer, k, v, dd, hint, tip in kpis))

    # ---------------- tightness diffusion index
    ti = brief.get("tightness") or {}
    if ti.get("available"):
        chips = "".join(
            '<div class="chip c-%s" data-tip="%s"><span class="cl">%s</span>'
            '<span class="cn">%s</span><span class="cv">%s</span></div>'
            % ("t" if c["tightening"] is True else ("e" if c["tightening"] is False else "f"),
               escape("%s · 来源 %s · %s · 本次 %s → 判为%s"
                      % (c["label"], c.get("source", ""), c.get("rule", ""),
                         ("%+.2f%%" % c["change_pct"]) if c["change_pct"] is not None else "无数据",
                         c["direction"])),
               escape(c["layer"]), escape(c["label"]),
               ("%+.2f%%" % c["change_pct"]) if c["change_pct"] is not None else "—")
            for c in ti["components"])
        # the score alone means nothing; spell out the recipe and every input
        ctab = table_of(
            ["分项", "所属层", "数据来源", "什么方向算「紧」", "本次变化", "判定"],
            [[c["label"], c["layer"], c.get("source", ""), c.get("rule", ""),
              ("%+.2f%%" % c["change_pct"]) if c["change_pct"] is not None else "无数据",
              c["direction"]] for c in ti["components"]])
        H.append(
            '<section class="panel tight" id="tightness"><div class="viewhead">'
            '<h2>AI 基础设施紧张度</h2><span class="badge">扩散指数 · 50 为中性</span></div>'
            '<div class="tigrid"><div class="tiscore"><div class="tiv">%.1f</div>'
            '<div class="tim">/ 100</div></div>'
            '<div class="tibar"><div class="tifill" style="width:%.1f%%"></div>'
            '<div class="titicks"><span>宽松</span><span>中性</span><span>紧张</span></div>'
            '<div class="tistat">紧张 %d · 宽松 %d · 持平 %d（共 %d 分项）</div></div></div>'
            '<div class="explain">'
            '<p><b>它是什么</b>：把下面 9 个分项各自和上一次采集比一下，'
            '数一数有几个朝「供需变紧」的方向动，算成 0–100 的占比。'
            '不是价格、不是金额，是<b>方向的票数</b>。</p>'
            '<p><b>怎么读</b>：50 = 一半紧一半松，中性。'
            '&gt;50 偏紧（需求在涨、或卡在变少、或成本在涨），&lt;50 偏松。'
            '它只说<b>有多少条线在同向移动</b>，不说幅度——'
            '9 个分项各涨 0.1%% 和各涨 10%%，指数都是 100。所以要配合上面的具体数值看。</p>'
            '<p><b>为什么等权、为什么不用 z-score</b>：等权是因为「哪一层更重要」没有客观答案，'
            '加权就是塞进一个未说明的观点。z-score 需要稳定的均值和标准差，'
            '观测数少的时候两者本身就是噪声，指数会无理由地剧烈摆动；'
            '而「方向」从第 2 次观测起就有定义。</p>'
            '</div>'
            '<div class="chips">%s</div>'
            '%s'
            '<p class="note">%s</p></section>'
            % (ti["value"], ti["value"], ti["tightening"], ti["easing"], ti["flat"],
               ti["n_live"], chips,
               '<details class="tbl"><summary>9 个分项的来源与判定规则</summary>%s</details>' % ctab,
               ("当前仅 %d 次观测，本指数基本等于「今天发生了什么」，尚不构成周期判断。"
                % meta.get("observations", 0))
               if meta.get("observations", 0) < 10 else escape(ti.get("note", ""))))

    H.append(blk("demand", "需求", "谁在用、用了多少",
                 "Token 用量趋势 · 19 个月长历史 · 应用级归因",
                 "Token 用量持续高增 → 集群利用率抬升 → 倒逼云厂追加 CapEx。若用量走平而价格仍在降 → 应用落地遇阻，CapEx 承压。厂商份额与应用榜进一步告诉你增量来自谁：新模型换代、还是同一批应用放量。"))

    # ---------------- volume
    H.append(panel(
        "Token 用量趋势", "滚动 %s 窗口 · 总量 vs 美国模型" % window,
        line_chart("vol", [series_of("total_tokens", "全部模型", "s1"),
                           series_of("us_tokens", "美国模型", "s2")],
                   "tokens", lambda v: "%.0fT" % (v / 1e12), zero_base=True),
        table_of(["日期", "全部 (T)", "美国 (T)", "美国份额"],
                 [[sdate(r), "%.2f" % ((num(r.get("total_tokens")) or 0) / 1e12),
                   "%.2f" % ((num(r.get("us_tokens")) or 0) / 1e12),
                   fmt_pct(num(r.get("us_token_share_pct")))] for r in summary[-30:]]),
        pid="volume",
        meta=spec(scope=u"OpenRouter 平台上全部模型的 token 调用量，滚动窗口，输入+输出相加，含免费档。「美国模型」指 OpenAI / Anthropic / Google / xAI / Meta / Microsoft / Nvidia / Amazon 八家。", calc=u"各模型 prompt_tokens + completion_tokens 直接求和，不做任何缩放。份额 = 美国模型量 ÷ 全站量。", src=u"OpenRouter 排行榜接口 frontend/v1/rankings/models，每日采集两次，同日重复取最后一次。", warn=u"只覆盖经 OpenRouter 路由的流量。企业直连官方 API、或走 Bedrock/Vertex 的都不在内，所以这不是全市场总量，美国模型真实份额远高于此处。")))
    H.append('<div class="legend"><span class="sw s1"></span>全部模型'
             '<span class="sw s2"></span>美国模型</div>')

    # ---------------- long history (daily series, backfilled to 2025-01-01)
    # Backfill lands in the `day` window regardless of which window the rest of
    # the page uses, so load it explicitly — otherwise 19 months of history is
    # invisible on the default weekly view.
    daily = load_summary(datadir, "day")
    if len(daily) > 30:
        dm = ((brief.get("llm") or {}).get("daily") or {}).get("metrics") or {}
        tt = dm.get("total_tokens") or {}
        first, last = sdate(daily[0]), sdate(daily[-1])

        # Three frequencies, all pre-rendered server-side and toggled by CSS.
        # Aggregating in the browser instead would mean reimplementing the
        # bucketing and rebuilding each chart's crosshair payload in JS; the
        # crosshair reads getBoundingClientRect() at event time, so a hidden
        # SVG needs no special handling and simply never fires.
        KEYS = ["total_tokens", "us_tokens"]
        NAMES = [("total_tokens", "全部模型", "s1"), ("us_tokens", "美国模型", "s2")]
        # min_days guards partial edge buckets: below it, weekday composition
        # alone drives the mean (a month that has only run to a Saturday reads
        # structurally light), so the point would be noise dressed as a level.
        # Both panels share these thresholds — now that they toggle together,
        # showing different period sets in each would be indefensible.
        FREQ = [("d", "日频", "日", 1, 2, None, 7),
                ("w", "周频", "周", 4, 9, None, 4),
                ("m", "月频", "月", 15, 40, lambda s: s[:7], 3)]
        lv_charts, lv_tables, lv_notes = [], [], []
        gr_charts, gr_tables, gr_notes = [], [], []
        counts = {}
        for code, flabel, ulabel, min_days, gapd, xf, win in FREQ:
            buck = periodise(daily, KEYS, code, min_days)
            counts[code] = len(buck)
            if not buck:
                continue
            unit = {"d": "当日", "w": "本周", "m": "本月"}[code]

            # ---- level chart
            ser = []
            for key, nm, role in NAMES:
                pts = [(k, m[key]) for k, m, _t, _n in buck if key in m]
                ser.append({"name": nm, "color_role": role, "points": pts})
            # the mean-based axis cannot show "how much in total this week", and
            # a value-only tipfmt cannot either, so the bucket total and day
            # count ride along as a per-point annotation. It has to name 全站
            # explicitly: the annotation is shared by both series' tooltips, so
            # on the 美国模型 marker an unlabelled "本月合计" would read as that
            # series' own total when it is the site-wide one.
            ann = None
            if code != "d":
                ann = {}
                for k, _m, t, n in buck:
                    tot = t.get("total_tokens")
                    if tot is not None:
                        ann[k] = "全站%s合计 %.2fT · %d 天" % (unit, tot / 1e12, n)
            lv_charts.append((code, flabel, line_chart(
                "hist_" + code, ser, "日均 tokens",
                lambda v: "%.0fT" % (v / 1e12), height=250, zero_base=True,
                gap_days=gapd, xfmt=xf, annot=ann,
                tipfmt=lambda v: "%.3fT/日" % (v / 1e12))))

            hdr = {"d": "日期", "w": "周(周一)", "m": "月份"}[code]
            if code == "d":
                # for a daily bucket the mean IS the day and n is always 1, so
                # those two columns would just repeat themselves
                lv_tables.append((code, table_of(
                    [hdr, "全部 (T)", "美国 (T)"],
                    [[k, "%.3f" % ((m.get("total_tokens") or 0) / 1e12),
                      "%.3f" % ((m.get("us_tokens") or 0) / 1e12)]
                     for k, m, _t, _n in reversed(buck[-40:])])))
            else:
                lv_tables.append((code, table_of(
                    [hdr, "全部 日均(T)", "美国 日均(T)", "%s合计(T)" % unit, "天数"],
                    [[k[:7] if code == "m" else k,
                      "%.3f" % ((m.get("total_tokens") or 0) / 1e12),
                      "%.3f" % ((m.get("us_tokens") or 0) / 1e12),
                      "%.2f" % ((t.get("total_tokens") or 0) / 1e12), "%d" % n]
                     for k, m, t, n in reversed(buck[-40:])])))
            lv_notes.append((code, {
                "d": "日频保留全部噪声，图中锯齿是周末效应（周末用量结构性低于工作日），不是数据问题。",
                "w": "按 ISO 周（周一至周日）归集，周末效应被周内平均吸收。",
                "m": "按自然月归集，最平滑，适合读趋势。",
            }[code]))

            # ---- growth chart, same buckets
            gr = growth_of(buck, code)
            if len(gr) < 3:
                continue
            gv = [g[1] for g in gr]
            # at daily frequency the day count is always 1 and the comparison
            # period is always yesterday, so spelling both out 575 times is
            # ~25KB of the page saying nothing
            if code == "d":
                rows_g = [(k[2:], g, "%s · 环比 %+.1f%% · %.3fT" % (k, g, mean / 1e12))
                          for k, g, mean, _n, _pk in gr]
            else:
                rows_g = [(k[2:] if code == "w" else k[2:7], g,
                           "%s · 环比 %+.1f%% · 日均 %.3fT · %d 天 · 对比 %s"
                           % (k, g, mean / 1e12, n, pk))
                          for k, g, mean, n, pk in gr]
            imax, imin = gv.index(max(gv)), gv.index(min(gv))
            gr_charts.append((code, flabel, dbar_chart(
                "growth_" + code, rows_g, "环比 %",
                yfmt=lambda v: "%+.0f%%" % v,
                label_idx={imax, imin, len(rows_g) - 1})))
            gr_tables.append((code, table_of(
                [hdr, "日均 (T)", "天数", "环比", "对比期"],
                [[k[:7] if code == "m" else k, "%.3f" % (mean / 1e12),
                  "%d" % n, "%+.1f%%" % g, pk[:7] if code == "m" else pk]
                 for k, g, mean, n, pk in reversed(gr)])))

            # Summarising a rate is where this goes wrong quietly. The arithmetic
            # mean of period-over-period percentages is biased upward when the
            # series is volatile, and averaging across weekday boundaries is
            # meaningless anyway: at daily frequency it returned ~-0.1%/day
            # (about -3%/month) for a month the monthly view scores at +27.6%.
            # Both cannot describe the same series.
            #
            # So compare LEVELS over three equal windows instead — no averaging
            # of returns, and at daily frequency a 7-day window cancels the
            # weekday effect by construction.
            lv = [x[1].get("total_tokens") for x in buck]   # bucket means, chronological
            summ = ""
            if len(lv) >= 3 * win and all(v is not None for v in lv[-3 * win:]):
                def wmean(a, b):
                    seg = lv[a:b] if b else lv[a:]
                    return (sum(seg) / len(seg)) if seg else None
                A, B, C = wmean(-win, None), wmean(-2 * win, -win), wmean(-3 * win, -2 * win)
                if A and B and C:
                    g1, g0 = (A / B - 1.0) * 100.0, (B / C - 1.0) * 100.0
                    # "增速抬升" on -2.1% against -6.0% reads as growing faster
                    # when the series is in fact shrinking less, so the verdict
                    # has to know about signs
                    if g1 >= 0 and g0 >= 0:
                        verdict = "增速抬升" if g1 > g0 else "增速放缓"
                    elif g1 < 0 and g0 < 0:
                        verdict = "跌幅收窄" if g1 > g0 else "跌幅扩大"
                    else:
                        verdict = "由跌转涨" if g1 >= 0 else "由涨转跌"
                    summ = ("近 %d%s vs 前 %d%s %+.1f%%，上一段 %+.1f%%（%s）　"
                            % (win, ulabel, win, ulabel, g1, g0, verdict))
            skipped = len(buck) - 1 - len(gr)
            gr_notes.append((code, "%s共 %d 个%s环比观测%s%s" % (
                summ, len(gr), ulabel,
                ("，%d 处因相邻期缺失未计算" % skipped) if skipped else "",
                "。日环比被周内构成主导（周六对周五天然是负的），"
                "极值基本是星期几造成的，不要当成事件。" if code == "d" else "。")))

        def freqbar(note):
            return ('<div class="freqbar" role="group" aria-label="频率口径">'
                    + "".join('<button type="button" class="fq%s" data-set="%s">%s</button>'
                              % (" on" if c == "d" else "", c, lb)
                              for c, lb, _s in lv_charts)
                    + '<span class="fqnote">%s</span></div>' % escape(note))

        def views(items):
            return "".join('<div data-freq="%s"%s>%s</div>'
                           % (c, "" if c == "d" else " hidden", body)
                           for c, body in items)

        # a growth multiple off two single days is hostage to which weekday each
        # landed on; take it off the monthly means instead
        mb = periodise(daily, ["total_tokens"], "m", 15)
        mult = ""
        if len(mb) >= 2:
            f0 = mb[0][1].get("total_tokens") or 0
            l0 = mb[-1][1].get("total_tokens") or 0
            if f0:
                mult = ("　月日均 %s→%s 涨 %.0f 倍"
                        % (mb[0][0][:7], mb[-1][0][:7], l0 / f0))

        GROUP = ' data-freqgroup="tokens"'
        H.append(panel(
            "Token 用量历史（D/W/M）",
            "%s 至 %s · 日 %d / 周 %d / 月 %d 个观测%s"
            % (first, last, counts.get("d", 0), counts.get("w", 0),
               counts.get("m", 0), mult),
            freqbar("三种频率同为「日均 token 量」，纵轴同一单位，切换只改变平滑程度，"
                    "不改变量级；下方增速面板同步切换")
            + views([(c, s) for c, _lb, s in lv_charts]),
            # frequency-independent, so it stays put rather than toggling away
            table_of(["指标", "日环比", "周环比", "月环比", "同比"],
                     [["日 Token 量", fmt_pct(tt.get("dod")), fmt_pct(tt.get("wow")),
                       fmt_pct(tt.get("mom")), fmt_pct(tt.get("yoy"))]])
            + views(lv_tables),
            note=views(lv_notes),
            pid="history", extra=GROUP,
            meta=spec(scope=u"每个自然日的全站 token 总量（不是滚动窗口），回补区间 2025-01-01 起。周频按 ISO 周（周一至周日）归集，月频按自然月归集。「美国模型」指 OpenAI / Anthropic / Google / xAI / Meta / Microsoft / Nvidia / Amazon 八家。", calc=u"接口每日返回用量前 50 的模型，外加一行 other 汇总其余全部模型，两者相加即当日完整总量，因此不是「只统计头部」。周频/月频取该桶内各日总量的算术平均即「日均」，而非桶内合计——合计口径会混入三种假信号：自然月天数差异（2 月→3 月仅凭 31/28 就多出 10.7%）、边缘桶未走完（当周/当月总是进行中，合计会塌陷成假暴跌）、以及 2 处缺日各让所在周少掉约 1/7。桶内合计仍在 tooltip 和数据表里给出。", src=u"OpenRouter 官方数据集 v1/datasets/rankings-daily，一次性回补，此后由日常采集接续。", warn=u"周频要求桶内至少 4 天、月频至少 15 天，不足则该桶不画（当周/当月刚开始时，日均会被周内构成主导）。所以最右端可能比日频少一个点，这是有意的。只有「量」能回补——OpenRouter 不提供历史价格，所以价格/支出/VWAP 只能从本看板首次运行起累积，本面板故意不画这三条。日频图中 2 处断点（2025-06-15、2025-07-15）是上游数据集本身的空洞，周频月频下它们被日均口径吸收，不再显示为断点。")))
        H.append('<div class="legend"><span class="sw s1"></span>全部模型'
                 '<span class="sw s2"></span>美国模型'
                 '<span class="lgnote">纵轴＝日均 tokens，三频率同单位</span></div>')

        if gr_charts:
            H.append(panel(
                "Token 用量环比增速（D/W/M）",
                "与上方用量历史同频联动 · 任一处切换，两图一起变",
                freqbar("环比＝本期日均 ÷ 上期日均 − 1；柱高是增速不是量级")
                + views([(c, s) for c, _lb, s in gr_charts]),
                views(gr_tables),
                note=views(gr_notes),
                pid="momgrowth", extra=GROUP,
                meta=spec(scope=u"上方面板同一批分桶的环比增速，频率随上方联动。日频＝逐日对前一日，周频＝逐周对上周，月频＝逐月对上月。", calc=u"每期先取该期「日均 token 量」＝桶内各日总量的算术平均，再算相邻两期日均之比减一。用日均而非桶内合计是必要的：合计口径的环比会混入自然月天数差异（2 月→3 月仅凭 31/28 就凭空多出 10.7%）与边缘桶未走完的假暴跌。只有当上一期是紧邻的前一期时才计算——跨过数据空洞去比，会得到一个算术正确但标签撒谎的「环比」，那种点直接跳过，留成真实的缺口。", src=u"与上方面板同源，OpenRouter v1/datasets/rankings-daily 回补 + 日常采集接续，不引入任何新数据源。", warn=u"频率越细，噪声越大：日环比几乎完全由星期几决定（周六对周五天然为负），读它的极值没有意义，看周频月频才是趋势。柱高是增速不是量级——增速回落只说明扩张变慢，不代表用量下降，用量本身见上方面板。")))
            H.append('<div class="legend"><span class="sw s1"></span>环比正增长'
                     '<span class="sw s2"></span>环比负增长'
                     '<span class="lgnote">柱高＝本期日均相对上期的变化幅度</span></div>')

    # ---------------- vendor share, the one series with a year already in it
    ms = brief.get("market_share") or {}
    if ms.get("available"):
        mvmap = {x["author"]: x for x in ms["movers"]}

        # every vendor, one mini chart each. 19 series cannot be separated by
        # colour, so each panel gets its own frame and a single hue instead.
        sm_panels = []
        for a in ms["authors"]:
            pts = [(w, v) for w, v in ms["series"][a]]
            if len(pts) < 2:
                continue
            # read the label straight off the series. Taking it from `movers`
            # showed `others` as 0.0% because that bucket is excluded there.
            cur_sh, first_sh = pts[-1][1], pts[0][1]
            sm_panels.append({
                "name": a, "head": a,
                "sub": "%.1f%%  %+.1f" % (cur_sh, cur_sh - first_sh),
                "points": pts, "_cur": cur_sh})
        # biggest current share first so the eye lands on what matters
        sm_panels.sort(key=lambda p: -p["_cur"])

        H.append(panel(
            "全部厂商：过去一年周度份额", "%d 家 × %d 周，每格一家厂商。格内右上为「当前份额  一年变动(百分点)」，纵轴统一刻度可横向比较"
            % (len(sm_panels), ms["weeks"]),
            small_multiples("msall", sm_panels, yfmt=lambda v: "%.1f%%" % v, cols=4),
            table_of(["厂商", "本周份额", "一年前", "一年变动", "本周环比"],
                     [[x["author"], "%.1f%%" % x["share_pct"],
                       "%.1f%%" % x["share_1y_ago"],
                       "%+.1fpct" % x["chg_1y_pct_points"],
                       "%+.1fpct" % x["chg_1w_pct_points"]] for x in ms["movers"]]),
            note="纵轴统一到全体最大值，所以小份额厂商的线看起来贴底——这是有意的，"
                 "各自缩放会让 0.5% 的厂商和 20% 的看起来一样高。",
            pid="mshare",
            meta=spec(
                scope="按厂商（模型作者）聚合的周度 token 量占比，%d 家 + 一个 others 残差桶，"
                      "最新一周 %s。" % (len(ms["authors"]), ms["last"]),
                calc="份额 = 该厂商当周 token 量 ÷ 当周全站量。小图纵轴统一到全体最大份额，"
                     "面积填充仅为可读性，无额外含义。",
                src="OpenRouter 排行榜市场份额接口 frontend/v1/rankings/market-share，免鉴权，"
                    "单次调用即返回 52 周完整历史，每日重取并整表覆盖。",
                warn="这是本看板唯一自带一年历史的序列，其余都只能从首次采集起累积。"
                     "口径仍是 OpenRouter 路由流量，不含企业直连第一方 API，"
                     "所以份额变化说的是开发者与 agentic coding 切片内的结构，"
                     "不等于这些厂商的真实市场份额。")))

        # eight named vendors on one axis: four overseas, four domestic.
        # Slot order is fixed and documented here; colour follows the vendor,
        # never its current rank, so a vendor keeps its hue as ranks shuffle.
        EIGHT = [("anthropic", "s1", "Anthropic", "海外"),
                 ("openai",    "s2", "OpenAI",    "海外"),
                 ("x-ai",      "s3", "xAI",       "海外"),
                 ("google",    "s4", "Google",    "海外"),
                 ("z-ai",      "s5", "智谱 GLM",  "国产"),
                 ("moonshotai","s6", "月之暗面 Kimi", "国产"),
                 ("deepseek",  "s7", "DeepSeek",  "国产"),
                 ("qwen",      "s8", "阿里 Qwen", "国产")]
        eight = [(a, role, disp, grp) for a, role, disp, grp in EIGHT
                 if ms["series"].get(a)]
        if eight:
            ser8 = [{"name": disp, "color_role": role,
                     "points": [(w, v) for w, v in ms["series"][a]]}
                    for a, role, disp, grp in eight]
            rows8 = []
            for a, role, disp, grp in eight:
                vs = [v for _, v in ms["series"][a]]
                rows8.append([disp, grp, "%.1f%%" % vs[-1], "%.1f%%" % vs[0],
                              "%.1f%%" % max(vs), "%+.1fpct" % (vs[-1] - vs[0])])
            H.append(panel(
                "八家主要厂商：一年周度份额", "海外四家（Anthropic / OpenAI / xAI / Google）"
                "对国产四家（智谱 / 月之暗面 / DeepSeek / Qwen）",
                line_chart("ms8", ser8, "份额 %", lambda v: "%.0f%%" % v,
                           height=330, gap_days=9, zero_base=True,
                           tipfmt=lambda v: "%.2f%%" % v, end_labels=True),
                table_of(["厂商", "阵营", "本周", "一年前", "期间峰值", "一年变动"], rows8),
                note="三家曾经的高位选手现已归零：xAI 峰值 40.8%、Qwen 25.6%、Kimi 12.4%，"
                     "当前均为 0.0%。这说明量端的位置极不稳固——单一模型换代就足以让一家厂商"
                     "从两位数份额掉出榜单。",
                pid="mshare8",
                meta=spec(
                    scope="八家指定厂商的周度份额，口径与上方全量图一致（按模型作者聚合）。",
                    calc="份额 = 该厂商当周 token 量 ÷ 当周全站量。线尾直接标注厂商名，"
                         "重叠时按 10px 最小间距上下错开。",
                    src="OpenRouter frontend/v1/rankings/market-share，52 周完整历史。",
                    warn="配色为固定槽位分配、与排名无关，所以厂商换位时颜色不变。"
                         "黄色与品红两槽在浅色底对比度低于 3:1，因此本图同时提供线尾直接标注"
                         "与数据表，不依赖颜色单独区分。")))
            H.append('<div class="legend">%s</div>' % "".join(
                '<span class="sw %s"></span>%s' % (role, escape(disp))
                for a, role, disp, grp in eight))

        # aggregate view, with the legend that was missing
        ser = [{"name": "美国系八家合计", "color_role": "s1",
                "points": [(w, v) for w, v in ms["us_series"]]},
               {"name": "其余全部（含 others 残差）", "color_role": "s2",
                "points": [(w, v) for w, v in ms["rest_series"]]}]
        H.append(panel(
            "美国系 vs 其余：份额此消彼长", "两条线合计恒为 100%，交叉点即量端易主的时点",
            line_chart("msh", ser, "份额 %", lambda v: "%.0f%%" % v,
                       height=230, gap_days=9, zero_base=True,
                       tipfmt=lambda v: "%.1f%%" % v),
            None,
            note="美国系八家 = OpenAI / Anthropic / Google / xAI / Meta / Microsoft / Nvidia / Amazon。"
                 "一年内从 %.1f%% 降到 %.1f%%。" % (ms["us_share_1y_ago"], ms["us_share_now"]),
            pid="mshare_agg"))
        H.append('<div class="legend"><span class="sw s1"></span>美国系八家合计'
                 '<span class="sw s2"></span>其余全部（含 others 残差）</div>')

        # direction must be visible: a single chart of absolute magnitudes made a
        # +19.8 gainer and a -15.3 loser look identical
        mv = [x for x in ms["movers"] if abs(x["chg_1y_pct_points"]) >= 0.5]
        def mvbar(items, role, cid):
            # bar length is the magnitude; the printed label keeps the sign, so a
            # -15.3pct loser never reads as +15.3
            return hbar_chart(cid,
                [(x["author"], abs(x["chg_1y_pct_points"]),
                  "%s · 现 %.1f%% · 一年前 %.1f%% · 一年 %+.1fpct · 本周环比 %+.1fpct"
                  % (x["author"], x["share_pct"], x["share_1y_ago"],
                     x["chg_1y_pct_points"], x["chg_1w_pct_points"]),
                  "%+.1f" % x["chg_1y_pct_points"])
                 for x in items],
                lambda v: "%+.1f" % v, color_role=role)
        up = sorted([x for x in mv if x["chg_1y_pct_points"] > 0],
                    key=lambda x: -x["chg_1y_pct_points"])[:9]
        dn = sorted([x for x in mv if x["chg_1y_pct_points"] < 0],
                    key=lambda x: x["chg_1y_pct_points"])[:9]
        H.append('<section class="panel"><h2>一年份额变动</h2>'
                 '<p class="sub">单位：百分点。柱长为变动幅度，数值带正负号</p>'
                 '<div class="two"><div><p class="mini">份额上升</p>%s</div>'
                 '<div><p class="mini">份额下降</p>%s</div></div>'
                 '<p class="note">上升集中在新进的中国模型（小米 MiMo、腾讯 Hy3、DeepSeek、MiniMax）；'
                 '下降侧除 Google、Anthropic、OpenAI 之外，也包括更早的中国领跑者 Qwen 与 Moonshot。'
                 '所以这是一次<strong>代际更替</strong>，不是简单的中美易位——'
                 '新模型同时从美国在位者和上一代中国模型手里拿量。</p></section>'
                 % (mvbar(up, "s3", "msup"), mvbar(dn, "s2", "msdn")))

    # ---------------- who burns the tokens
    ap_ = brief.get("apps") or {}
    if ap_.get("available"):
        ov = ap_["overall"][:15]
        rows = [(a["app"][:28], (a["tokens"] or 0) / 1e12,
                 "%s · %.2fT tokens · %s 次请求" % (a["app"], (a["tokens"] or 0) / 1e12,
                                                  "{:,.0f}".format(a["requests"] or 0)))
                for a in ov]
        cli = ap_["by_subcategory"].get("cli-agent") or []
        ide = ap_["by_subcategory"].get("ide-extension") or []
        sub = ""
        if cli or ide:
            mk = lambda L: hbar_chart("sc", [(x["app"][:24], (x["tokens"] or 0) / 1e12,
                                              "%s · %.2fT" % (x["app"], (x["tokens"] or 0) / 1e12))
                                             for x in L[:8]], lambda v: "%.2fT" % v, color_role="s3")
            sub = ('<div class="two"><div><p class="mini">CLI Agent</p>%s</div>'
                   '<div><p class="mini">IDE 插件</p>%s</div></div>' % (mk(cli), mk(ide)))
        H.append(panel(
            "谁在烧 token", "近 %d 天，按应用口径。Top10 占 %s%%"
            % (ap_.get("window_days", 0), ap_.get("top10_share_pct")),
            hbar_chart("apps", rows, lambda v: "%.2fT" % v, color_role="s1", width=860),
            table_of(["#", "应用", "Tokens", "请求数"],
                     [[a["rank"], a["app"], "%.3fT" % ((a["tokens"] or 0) / 1e12),
                       "{:,.0f}".format(a["requests"] or 0)] for a in ap_["overall"]]),
            pid="apps",
            meta=spec(scope=u"按「调用方应用」而非按模型归因的 token 量。只含在 OpenRouter 应用市场公开可见的应用。", calc=u"应用身份来自调用方自报的 HTTP-Referer / X-Title 头，OpenRouter 按此聚合并合并同一应用的别名。占比分母是本榜单合计，不是全站总量（可归因部分约占全站四分之一）。", src=u"OpenRouter 官方数据集 v1/datasets/app-rankings，需免费 API key，每日采全类目 + coding 四个子类。", warn=u"隐藏应用与私有应用不计入；自建脚本若不上报身份也不会出现。所以这是「愿意署名的应用」排行。")) + sub)
    else:
        H.append('<section class="panel placeholder" id="apps"><h2>谁在烧 token</h2>'
                 '<p class="sub">应用级需求归因：Cursor / Cline / CLI agent 等</p>'
                 '<div class="ph">待接入 <code>OPENROUTER_API_KEY</code></div>'
                 '<p class="note">%s</p></section>' % escape(ap_.get("note", "")))


    H.append(blk("revenue", "价格与收入", "卖多少钱、钱归了谁",
                 "三种价格口径 · 周度支出 · 量与钱的背离",
                 "价格降 + 用量涨 → 效率提升带来的真实需求扩张。价格战 + 用量走平 → 推理容量过剩，先压缩 neocloud 续租，再传导到服务器与 GPU 订单。注意看无权重均价与 VWAP 的裂口：只有后者动才是成交结构真的变了。"))
    # ---------------- three price measures
    H.append(panel(
        "三种价格口径", "同为 $/Mn tokens，同轴可比。无权重均值反映挂牌分布，VWAP 才是实际成交单位经济",
        line_chart("px", [series_of("avg_list_price_usd_per_mn", "无权重列表均价", "s2"),
                          series_of("vwap_usd_per_mn", "VWAP 实际成交", "s1"),
                          series_of("us_vwap_usd_per_mn", "美国模型 VWAP", "s3")],
                   "$/Mn", lambda v: "$%.2f" % v, zero_base=True),
        table_of(["日期", "无权重均价", "VWAP", "美国 VWAP"],
                 [[sdate(r), fmt_price(num(r.get("avg_list_price_usd_per_mn"))),
                   fmt_price(num(r.get("vwap_usd_per_mn"))),
                   fmt_price(num(r.get("us_vwap_usd_per_mn")))] for r in summary[-30:]]),
        pid="price",
        meta=spec(scope=u"三条线都是「每 100 万 token 多少美元」，同轴可比但统计对象不同：① 无权重列表均价＝把每个在售模型的挂牌价简单平均；② VWAP＝全站实际成交均价；③ 美国模型 VWAP＝仅八家美国厂商模型的成交均价。", calc=u"① 每模型 (输入价+输出价)/2 后算术平均，每个模型权重相同、无论有没有流量；② 估算支出 ÷ 付费 token 数，即按每模型真实输入/输出配比加权。免费档不计入 ② ③。", src=u"用量来自 OpenRouter 排行榜接口，单价来自官方价目表 v1/models（每日另存一份原始快照以便日后重算）。", warn=u"① 高于 ② 通常 3–6 倍，这不是矛盾：① 被 o1-pro（$375/Mn）这类高价低量模型拉高，反映「在售模型的价格分布」；② 才是真实单位经济。根因是实际流量约 29:1 输入重（输出 token 仅占 3%），任何用 (输入+输出)/2 中点的算法都会高估实际单价。")))
    H.append('<div class="legend"><span class="sw s2"></span>无权重列表均价'
             '<span class="sw s1"></span>VWAP 实际成交<span class="sw s3"></span>美国模型 VWAP</div>')

    H.append(sechead("支出", "Token 支出与结构", "量、钱分布与集中度"))
    # ---------------- spend
    H.append(panel(
        "周度 Token 支出", "按 OpenRouter 列表价估算，不含企业折扣与缓存定价，属上限估计",
        bar_chart("sp", [sdate(r)[5:] for r in summary[-40:]],
                  [(num(r.get("est_spend_usd")) or 0) / 1e6 for r in summary[-40:]],
                  "$M", lambda v: "$%.0fM" % v, color_role="s1"),
        table_of(["日期", "总支出", "美国支出", "美国份额"],
                 [[sdate(r), fmt_usd(num(r.get("est_spend_usd"))),
                   fmt_usd(num(r.get("est_us_spend_usd"))),
                   fmt_pct(num(r.get("us_spend_share_pct")))] for r in summary[-30:]]),
        pid="spend",
        meta=spec(scope=u"滚动窗口内全站付费档 token 折算出的美元金额。免费档记 0。", calc=u"逐模型：输入 token 数 × 该模型输入单价 ＋ 输出 token 数 × 输出单价，然后求和。输入输出分开计价，不用中点近似。", src=u"用量 × OpenRouter 官方挂牌价，两者均每日采集。", warn=u"这是上限估计。挂牌价不含企业与承诺用量折扣、批量 API（通常 5 折）、以及缓存命中（实测中位只按输入价的 10% 计费）。输入 token 占 97% 且 agentic 工具反复复用 context，缓存这一项影响最大——敏感性测算下真实值可能只有此数一半。引用时说「不超过」，不要说「花了」。")))

    # ---------------- top models
    if models:
        def key(r):
            return r["model"].split("/")[-1][:26] + (" :free" if r.get("variant") == "free" else "")
        byv = sorted(models, key=lambda r: -(num(r.get("total_tokens")) or 0))[:15]
        bys = sorted([r for r in models if num(r.get("est_spend_usd"))],
                     key=lambda r: -(num(r.get("est_spend_usd")) or 0))[:15]
        lv = hbar_chart("tv", [(key(r), (num(r.get("total_tokens")) or 0) / 1e12,
                                "%s · %.2fT tokens · %s" % (key(r), (num(r.get("total_tokens")) or 0) / 1e12,
                                                            fmt_usd(num(r.get("est_spend_usd")))))
                               for r in byv], lambda v: "%.1fT" % v)
        ls = hbar_chart("ts", [(key(r), (num(r.get("est_spend_usd")) or 0) / 1e6,
                                "%s · %s · %.2fT tokens" % (key(r), fmt_usd(num(r.get("est_spend_usd"))),
                                                            (num(r.get("total_tokens")) or 0) / 1e12))
                               for r in bys], lambda v: "$%.1fM" % v, color_role="s2")
        H.append('<section class="panel"><h2>量与钱的背离</h2>'
                 '<p class="sub">左：Token 量前 15（%s）　右：估算支出前 15</p>'
                 '<div class="two"><div><p class="mini">按 Token 量</p>%s</div>'
                 '<div><p class="mini">按估算支出</p>%s</div></div>'
                 '<p class="note">榜单几乎不重叠：中国开源模型供给体积，Anthropic / OpenAI 收走收入。</p>'
                 '%s</section>' % (escape(mdate), lv, ls, spec(
                     scope="同一批模型、同一时点，分别按 token 量和按估算支出排前 15。",
                     calc="左图值 = 该模型周内 token 总量；右图值 = 输入token×输入价 + 输出token×输出价。"
                          "两张图取自同一份快照，名次差异只来自单价，不来自时点。",
                     src="用量 OpenRouter 排行榜接口 × 单价 OpenRouter 官方价目表。",
                     warn="左图含免费档（标 :free），右图不含——免费档支出恒为 0，"
                          "所以它出现在量榜而不出现在钱榜是口径必然，不是异常。")))

        # ---------------- scatter
        pts = [((num(r.get("total_tokens")) or 0) / 1e9,
                num(r.get("list_price_blended_usd_per_mn")),
                r["model"].split("/")[-1])
               for r in models
               if r.get("variant") != "free" and num(r.get("list_price_blended_usd_per_mn"))]
        H.append(panel(
            "价格 vs 用量", "%d 个付费模型，双对数轴。复刻 JPM Figure 6" % len(pts),
            scatter_chart("sc", pts, "周 Token 用量（十亿，对数）", "实际成交单价 $/Mn（对数）"),
            pid="scatter",
            meta=spec(scope=u"每个付费模型一个点：横轴为该模型近一周 token 用量，纵轴为其实际成交单价。双对数轴。", calc=u"纵轴 = 该模型 (输入token×输入价 + 输出token×输出价) ÷ 总token，即它自己的真实混合单价。免费档与路由型模型（openrouter/auto 等挂牌价为 -1 哨兵值）已剔除。", src=u"用量 OpenRouter 排行榜接口 × 单价 OpenRouter 官方价目表，同日快照。", warn=u"四角标注的是价量两端的极值模型，不是推荐。点的疏密只反映在售模型分布，不代表市场结构——高价低量的长尾模型数量多但几乎不产生流量。")))

    H.append(blk("cost", "成本", "算力与存储的价钱",
                 "neocloud · 超大规模云 · 供给紧张度 · DRAM/NAND",
                 "租金上行 + 可用率下探 → 算力吃紧，利好上游订单。租金持续下滑 → 供给过剩 → neocloud 砍单 → 传导至服务器 ODM 与 NVIDIA。可用率通常先于价格转向（新卡先零可用、后涨价），是本层里最早的那个信号。存储现货同步走高则说明整机 BOM 成本在抬，压缩系统厂毛利。"))
    # ---------------- memory
    H.append(sechead("存储", "DRAM / NAND 现货",
                     "DRAM 日更、NAND 周更，两者变化率不可并列解读"))
    mem = brief.get("memory") or {}
    if mem:
        dram = [(k, v) for k, v in mem.items() if v.get("kind") == "dram"]
        nand = [(k, v) for k, v in mem.items() if v.get("kind") == "nand"]
        roles = ["s1", "s2", "s3"]
        if memory_absolute:
            mk = lambda vv: [(a, b) for a, b in vv["history"]]
            ylab, yfmt = "$", (lambda v: "$%.0f" % v)
        else:
            mk = lambda vv: [(a, b) for a, b in vv["indexed"]]
            ylab, yfmt = "指数（首次观测=100）", (lambda v: "%.0f" % v)
        ser = [{"name": k, "color_role": roles[i % 3], "points": mk(v)}
               for i, (k, v) in enumerate(dram[:3])]
        H.append(panel(
            "DRAM 现货（日更）", "TrendForce 现货收盘，GMT+8 18:10 更新",
            line_chart("dram", ser, ylab, yfmt),
            table_of(["品类", "最新", "较上次", "数据源时间"],
                     [[k, ("%.3f" % v["value"]) if memory_absolute else ("%.1f" % (v["indexed"][-1][1] if v["indexed"] else 0)),
                       fmt_pct(v.get("dod")), v.get("source_update", "")] for k, v in dram]),
            pid="dram",
            meta=spec(scope=u"DRAM 现货报价，取 TrendForce 每日收盘的 session average。默认显示指数化（首次观测=100）。", calc=u"直接取报价，不做平滑。指数化 = 当日值 ÷ 首次观测值 × 100，便于与 NAND 同轴比较。", src=u"TrendForce 公开现货页，每日 18:10（GMT+8）收盘更新，本看板 20:00 那一跑抓取。", warn=u"绝对报价属 TrendForce 商业数据，页面只显示指数与变化率，原始价位保留在本地 CSV。要显示绝对值用 build_site.py --memory-absolute。")))
        if nand:
            ser2 = [{"name": k, "color_role": roles[i % 3], "points": mk(v)}
                    for i, (k, v) in enumerate(nand[:3])]
            H.append(panel(
                "NAND 现货（周更）", "更新频率仅为 DRAM 的 1/5，变化率不可与 DRAM 并列解读",
                line_chart("nand", ser2, ylab, yfmt, gap_days=9),
                table_of(["品类", "最新", "较上次", "数据源时间"],
                         [[k, ("%.3f" % v["value"]) if memory_absolute else ("%.1f" % (v["indexed"][-1][1] if v["indexed"] else 0)),
                           fmt_pct(v.get("dod")), v.get("source_update", "")] for k, v in nand]),
                pid="nand",
                meta=spec(scope=u"NAND 闪存现货报价（512Gb / 256Gb / 128Gb TLC），同样取 session average。", calc=u"同 DRAM。", src=u"TrendForce 公开现货页。", warn=u"NAND 现货每周才更新一次，DRAM 是每日——两者变化率不可并列解读。JPM 报告把两者画在同一条月度轴上，掩盖了这个频率差。另外免费页最高只到 512Gb，JPM 用的 1Tb 需付费源，此处以 512Gb 代理。")))

    # ---------------- GPU market (Shadeform) + supply tightness
    H.append(sechead("算力", "GPU 租赁市场", "Shadeform 聚合 ~18 家 neocloud，与 JPM 所用 Silicon Data neo 口径同源"))
    if gm:
        order = ["B300", "B200", "H200", "H100", "H100_nvl", "A100", "A100_80G",
                 "L40S", "A6000", "RTX4090"]
        keys = [g for g in order if g in gm] + [g for g in sorted(gm) if g not in order]
        rb = [(g, gm[g]["min"], gm[g]["value"], gm[g]["max"],
               "%s · 中位 $%.2f · 区间 $%.2f–$%.2f · %d 家云 / %d 个报价"
               % (g, gm[g]["value"], gm[g]["min"], gm[g]["max"], gm[g]["clouds"], gm[g]["offers"]))
              for g in keys]
        av = [(g, gm[g].get("availability_pct") or 0.0,
               "%s · %s%% 区域有货" % (g, gm[g].get("availability_pct")))
              for g in keys]
        H.append('<section class="panel"><h2>租赁价与供给紧张度</h2>'
                 '<p class="sub">左：$/GPU-hour，圆点为跨云中位数，横线为最低到最高报价区间　右：有货区域占比</p>'
                 '<div class="two"><div><p class="mini">价格区间</p>%s</div>'
                 '<div><p class="mini">可用率</p>%s</div></div>'
                 '<p class="note">新一代零可用（B200/B300 全线无货）而低端仍有余量，'
                 '是供给紧张度的梯度信号——这一维度 JPM 报告完全没有。</p>%s</section>'
                 % (range_bar("gpx", rb, lambda v: "$%.2f" % v, "s1"),
                    hbar_chart("gav", av, lambda v: "%.0f%%" % v, color_role="s3"),
                    spec(
                     scope="左：各型号 neocloud 时租，圆点为跨云中位、横线为最低到最高报价。"
                           "右：该型号在所有被监测云区域中标记为「有货可租」的区域占比。",
                     calc="单卡时租 = 整机报价 ÷ 该机型 GPU 数，再对同型号所有报价取中位数。"
                          "可用率 = 有货区域数 ÷ 被监测区域总数（同型号所有机型的区域合并计算）。",
                     src="Shadeform 聚合接口 v1/instances/types，免鉴权，约 18 家 neocloud，每日两采。",
                     warn="可用率是价格的领先指标：新卡通常先零可用、后涨价。但它是瞬时快照、日内会变"
                          "（实测 A100 曾在数小时内从 13.1% 降到 9.8%），图上每日只保留最后一次采集。")))
        ser = [{"name": g, "color_role": ["s1", "s2", "s3"][i % 3],
                "points": [(a, b) for a, b in gm[g]["history"]]}
               for i, g in enumerate([k for k in ("H100", "A100", "B200") if k in gm])]
        H.append(panel("租赁价走势", "H100 / A100 / B200 跨云中位数",
                       line_chart("gput", ser, "$/GPU-hour", lambda v: "$%.2f" % v),
                       table_of(["GPU", "中位价", "最低", "最高", "可用率", "较上次"],
                                [[g, "$%.2f" % gm[g]["value"], "$%.2f" % gm[g]["min"],
                                  "$%.2f" % gm[g]["max"],
                                  ("%.0f%%" % gm[g]["availability_pct"]) if gm[g].get("availability_pct") is not None else "—",
                                  fmt_pct(gm[g].get("dod"))] for g in keys]),
                       pid="gpu",
                       meta=spec(scope=u"neocloud（中小型 GPU 云）的 GPU 时租价，跨云中位数，单位 $/GPU-hour。", calc=u"把每个机型报价除以其 GPU 数得到单卡时租，再对同型号所有报价取中位数。上游返回的 hourly_price 单位是「分/整机」，已折算。", src=u"Shadeform 聚合接口 v1/instances/types，免鉴权，覆盖约 18 家云；每日采集两次。", warn=u"同型号跨云价差常达 3–4 倍，所以用中位数而非单一报价。与超大规模云挂牌价不可直接比——后者贵约 6 倍，见下一面板。单位换算已用 Lambda Labs 公开价交叉验证（A100 $1.99、H100 $3.99–4.36 均吻合）。")))
        H.append('<div class="legend"><span class="sw s1"></span>H100'
                 '<span class="sw s2"></span>A100<span class="sw s3"></span>B200</div>')
    else:
        H.append('<section class="panel placeholder"><h2>GPU 租赁价</h2>'
                 '<div class="ph">尚未采集，运行 shadeform.py</div></section>')

    gpu = brief.get("gpu") or {}
    if gpu.get("status") != "live":
        H.append('<p class="asidenote">另有 Silicon Data GPU Index（Bloomberg/LSEG 分发，'
                 'index_version=neo，可回溯 2024-09-01）为付费源，设 <code>SD_TOKEN</code> 后自动并列显示以交叉校验。</p>')

    # ---------------- hyperscaler tier (Azure list prices)
    hs = brief.get("hyperscaler") or {}
    if hs:
        order = ["H100", "H200", "A100", "A100_40G", "MI300X", "H100_1x"]
        keys = [g for g in order if g in hs] + [g for g in sorted(hs) if g not in order]
        rows = []
        for g in keys:
            v = hs[g]
            neo = (gm.get(g) or {}).get("value")
            rows.append([g,
                         "$%.2f" % v["value"],
                         ("$%.2f" % v["lowpri"]) if v.get("lowpri") else "—",
                         ("$%.2f" % v["spot"]) if v.get("spot") else "—",
                         ("$%.2f" % neo) if neo else "—",
                         ("%.1fx" % (v["value"] / neo)) if neo else "—",
                         v["regions"]])
        # range_bar takes (label, lo, mid, hi): whisker spans the cheapest
        # evictable tier up to the on-demand list, dot marks on-demand — the
        # only tier that is a real citable number.
        rb = [(g,
               hs[g].get("spot") or hs[g].get("lowpri") or hs[g]["value"],
               hs[g]["value"],
               hs[g]["value"],
               "%s · 按需 $%.2f · LowPri %s · Spot %s · %d 个区域报价"
               % (g, hs[g]["value"],
                  ("$%.2f" % hs[g]["lowpri"]) if hs[g].get("lowpri") else "n/a",
                  ("$%.2f" % hs[g]["spot"]) if hs[g].get("spot") else "n/a",
                  hs[g]["regions"]))
              for g in keys]
        H.append(panel(
            "超大规模云挂牌价（Azure）", "圆点为按需挂牌中位价，横线左端为最便宜的可抢占档（Spot）",
            range_bar("hsx", rb, lambda v: "$%.2f" % v, "s2"),
            table_of(["GPU", "按需挂牌", "LowPri", "Spot", "neocloud", "按需/neo", "区域数"], rows),
            note=("<strong>Low Priority</strong> 经验上正好框住已公开的超大规模云「合约价」指数，"
                  "<strong>Spot</strong> 落在 neocloud 水平附近，"
                  "<strong>按需挂牌</strong>是唯一可引用的真实序列。"),
            pid="hyperscaler",
            meta=spec(scope=u"超大规模云（Azure）的 GPU 挂牌价，三档分开：按需、Low Priority（可抢占）、Spot。已按每台机器的 GPU 数折算为 $/GPU-hour。", calc=u"对同一 SKU 在全部区域的报价取中位数；按 meterName 是否含 Spot / Low Priority 分档。", src=u"Azure 零售价 API prices.azure.com/api/retail/prices，免鉴权，每日采集。", warn=u"三档差约 5 倍，混用会得出相反结论。企业协议价介于 Spot 与按需之间，任何地方都不公开。AWS 的等价数据是 140MB 单文件需流式解析，GCP 需 key，故只接 Azure。")))

    # ---------------- the bridge
    if br:
        H.append(blk("unit", "单位经济", "到底赚不赚钱",
                     "把价格、吐吐与 GPU 租金接在一起",
                     "这一层决定上面几层能不能持续。平衡并发上行 → 单位经济恶化 → 低价模型要么涨价要么退出，随之而来的是 token 价格见底与供给收缩。平衡并发下行 → 推理毛利扩张 → 有余力继续降价抢量，需求端还能再涨一轮。"))
        rows = [(m["model"].split("/")[-1][:30], m["break_even_streams"],
                 "%s · %.0f tok/s · $%.2f/Mn 输出 · 单流每小时创收 $%.3f · 每 GPU 等效需 %.1f 路"
                 % (m["model"].split("/")[-1], m["tok_s"], m["out_price_usd_per_mn"],
                    m["revenue_per_stream_hour"], m["break_even_streams"]))
                for m in br["models"]]
        H.append('<section class="panel" id="bridge"><h2>每 GPU 等效要跑多少路并发才回本</h2>'
                 '<p class="sub">%s 中位租金 $%.2f/hr · 数据截至 %s · 对数轴，越短越赚钱</p>'
                 '%s'
                 '<p class="note"><strong>刻意做成反解</strong>——若改算「每 token 成本」'
                 '就必须假设 batch size，那是不可观测的量且会主导结果，'
                 '正是让卖方支出估算失真的同一类隐藏假设。</p>'
                 '%s%s</section>'
                 % (escape(br["gpu_ref"]), br["gpu_usd_per_hour"],
                    escape(br["as_of"]), bridge_bar("br", rows),
                    spec(
                     scope="按「单张 GPU」归一的盈亏平衡并发，覆盖 20 个高流量模型（含闭源）。",
                     calc="单流每小时创收 = 单流 tok/s × 3600 × 输出单价；"
                          "本值 = H100 跨云中位时租 ÷ 该值。",
                     src="tok/s 来自 OpenRouter 各 provider 实测（按 30 分钟窗口请求数加权）；"
                         "输出单价取各 provider 中位；GPU 时租来自 Shadeform。",
                     warn="这一口径假设模型跑在一张卡上，实际不成立——真实数见下一面板的每副本修正。"
                          "闭源模型不公开参数量因而无法修正，所以只在这张图里出现。"),
                    '<details class="tbl"><summary>数据表</summary>%s</details>' % table_of(
                        ["模型", "tok/s", "$输出/Mn", "单流$/hr", "每GPU并发", "卡数", "每副本并发"],
                        [[m["model"].split("/")[-1], "%.1f" % m["tok_s"],
                          "%.3f" % m["out_price_usd_per_mn"],
                          "%.3f" % m["revenue_per_stream_hour"],
                          "%.1f" % m["break_even_streams"],
                          m["gpus_per_replica"] or "—",
                          ("%.0f" % m["break_even_streams_replica"])
                          if m["break_even_streams_replica"] else "闭源未知"]
                         for m in br["models"]])))

        # the per-replica correction, which reverses the per-GPU ranking
        rep = [m for m in br["models"] if m.get("break_even_streams_replica")]
        if rep:
            rep.sort(key=lambda m: -m["break_even_streams_replica"])
            rrows = [(("%s (%d卡)" % (m["model"].split("/")[-1][:22], m["gpus_per_replica"])),
                      m["break_even_streams_replica"],
                      "%s · %.0fB 参数 %s · %d 卡/副本 · 每 GPU %.1f 路 → 每副本 %.0f 路"
                      % (m["model"].split("/")[-1], m["params_b"] or 0, m["quantization"],
                         m["gpus_per_replica"], m["break_even_streams"],
                         m["break_even_streams_replica"]))
                     for m in rep]
            H.append(panel(
                "按真实副本规模修正后", "开源模型可算（%d 个）· 中位 %s 路/副本"
                % (br["n_with_replica"], br["median_break_even_replica"]),
                bridge_bar("brr", rrows),
                None,
                note=("模型不跑在一张卡上。参数量取自 HuggingFace safetensors，服务精度取自 "
                      "OpenRouter 各 provider 实际 quantization 字段（实测以 fp8 为主，"
                      "非发布时的 bf16），单卡 80GB、权重可用 60%（其余留给 KV cache 与激活），"
                      "修正后中位落到 " + str(br["median_break_even_replica"]) +
                      " 路，正好在真实服务并发区间内——意味着这些低价开源模型在挂牌价下"
                      "贴近盈亏线，而非每 GPU 口径显示的宽裕。"),
                pid="bridge_replica",
                meta=spec(scope=u"一个模型副本要同时服务多少路并发请求，token 收入才够付它占用的那批 GPU 的租金。仅开源模型可算。", calc=u"单流每小时创收 = 单流 tok/s × 3600 × 输出单价；每 GPU 平衡并发 = H100 时租 ÷ 该值；再乘每副本 GPU 数。副本卡数 = 参数量 × 每参数字节数 ÷ (80GB × 60%)，向上取到 2 的幂。", src=u"tok/s 来自 OpenRouter 各 provider 实测（按请求数加权）；参数量来自 HuggingFace safetensors；服务精度来自 OpenRouter 各 provider 的 quantization 字段（实测以 fp8 为主，非发布时的 bf16）；GPU 时租来自 Shadeform。", warn=u"三个已知偏差：① 只计输出 token 收入，而输入 token 是其约 29 倍的量且单独计费，因此系统性低估真实创收；② 用挂牌价，不含企业折扣与缓存折扣；③ 80GB 里留 60% 给权重是个假设（其余给 KV cache 与激活），长上下文场景会更紧。所以绝对值不可当毛利率读，跨模型比较与随时间的方向才有效。")))
        H.append('<p class="asidenote">%s</p>' % escape(br["note"]))

    # ---------------- equity complex
    eq = brief.get("equity") or {}
    if eq.get("available"):
        H.append(blk("market", "资本市场", "股价怎么反应",
                     "物理指标先动，股价后反应",
                     "上面五层是物理量，这一层是预期。物理指标先动、股价后反应，所以两者的落差本身就是信号：物理转紧而股价未动 = 预期滞后；物理转松而股价仍高 = 预期透支。融资成本（若接入债券层）走阔会直接压制后续 CapEx。"))
        gs = sorted(eq["groups"].items(), key=lambda kv: (kv[1]["chg_1mo_pct"] is None,
                                                          kv[1]["chg_1mo_pct"] or 0))
        grows = [(g, abs(v["pct_below_52w_high"] or 0),
                  "%s · %d 只 · 近1月均 %s%% · 距52周高均 %s%%"
                  % (g, v["n"], v["chg_1mo_pct"], v["pct_below_52w_high"]))
                 for g, v in sorted(eq["groups"].items(),
                                    key=lambda kv: -(kv[1]["pct_below_52w_high"] or 0))]
        H.append(panel(
            "各环节距 52 周高点回撤", "按产业链位置分组，均值。回撤越深说明该环节被定价出的周期越多",
            hbar_chart("eqg", grows, lambda v: "%.0f%%" % v, color_role="s2", width=860),
            table_of(["代码", "环节", "价格", "1日%", "近1月%", "距52周高%"],
                     [[n["ticker"], n["group"], "%.2f" % (n["price"] or 0),
                       n["chg_1d_pct"], n["chg_1mo_pct"], n["pct_below_52w_high"]]
                      for n in eq["names"]]),
            note=None, pid="equity",
            meta=spec(
                scope="13 只 AI 基础设施标的，按产业链环节分组（算力芯片 / 定制ASIC / 代工 / "
                      "存储 / 网络 / 光模块 / 电源散热 / 服务器）。",
                calc="日涨跌取序列最后两个收盘价之比；月涨跌取窗口首末收盘价之比；"
                     "距 52 周高点 =（52周高 − 现价）÷ 52周高。分组值为组内算术平均。",
                src="Yahoo Finance chart 接口，1 个月日线，每日采集。",
                warn="只有价格与 52 周区间。Consensus 预期与盈利预测调整需付费终端"
                     "（Bloomberg/FactSet/LSEG），本层不提供也不假装有；"
                     "Short interest 是 FINRA 双月度数据，不适合日频面板。"
                     "物理指标（GPU 价、可用率、存储）通常先动、股价后反应，"
                     "放在一起是为了看这个落差。")))

    asof = meta.get("as_of") or latest.get("as_of", "")
    nav, body = assemble(H)
    return PAGE % {
        "css": render_css(), "js": JS,
        "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "asof": escape(asof),
        "obs": meta.get("observations", len(summary)),
        "nav": nav,
        "body": body,
    }


CSS = """
:root{color-scheme:light dark}
.viz{--surface:%(surface_l)s;--plane:%(plane_l)s;--ink:%(ink_l)s;--ink2:%(ink2_l)s;
 --muted:%(muted_l)s;--grid:%(grid_l)s;--axis:%(baseline_l)s;--s1:%(s1_l)s;--s2:%(s2_l)s;
 --s3:%(s3_l)s;--s4:%(s4_l)s;--s5:%(s5_l)s;--s6:%(s6_l)s;--s7:%(s7_l)s;--s8:%(s8_l)s;--seq1:%(seq1_l)s;--seq2:%(seq2_l)s;--seq3:%(seq3_l)s;--good:%(good_l)s;--bad:%(bad_l)s;--ring:rgba(11,11,11,.10)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .viz{
 --surface:%(surface_d)s;--plane:%(plane_d)s;--ink:%(ink_d)s;--ink2:%(ink2_d)s;
 --muted:%(muted_d)s;--grid:%(grid_d)s;--axis:%(baseline_d)s;--s1:%(s1_d)s;--s2:%(s2_d)s;
 --s3:%(s3_d)s;--s4:%(s4_d)s;--s5:%(s5_d)s;--s6:%(s6_d)s;--s7:%(s7_d)s;--s8:%(s8_d)s;--seq1:%(seq1_d)s;--seq2:%(seq2_d)s;--seq3:%(seq3_d)s;--good:%(good_d)s;--bad:%(bad_d)s;--ring:rgba(255,255,255,.10)}}
:root[data-theme=dark] .viz{--surface:%(surface_d)s;--plane:%(plane_d)s;--ink:%(ink_d)s;
 --ink2:%(ink2_d)s;--muted:%(muted_d)s;--grid:%(grid_d)s;--axis:%(baseline_d)s;--s1:%(s1_d)s;
 --s2:%(s2_d)s;--s3:%(s3_d)s;--s4:%(s4_d)s;--s5:%(s5_d)s;--s6:%(s6_d)s;--s7:%(s7_d)s;--s8:%(s8_d)s;--seq1:%(seq1_d)s;--seq2:%(seq2_d)s;--seq3:%(seq3_d)s;--good:%(good_d)s;--bad:%(bad_d)s;--ring:rgba(255,255,255,.10)}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
 font:14px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:22px 16px 60px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
h1{font-size:20px;margin:0;letter-spacing:-.01em}
.meta{color:var(--muted);font-size:12px}
#tt{margin-left:auto;background:none;border:1px solid var(--ring);color:var(--ink2);
 border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px}
.panel{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
 padding:16px 18px;margin:14px 0}
h2{font-size:15px;margin:0 0 2px}
.sub{color:var(--ink2);font-size:12.5px;margin:0 0 10px}
.mini{color:var(--ink2);font-size:12px;margin:0 0 4px}
.note{color:var(--muted);font-size:11.5px;margin:8px 0 0;line-height:1.5}
.chart{width:100%%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--grid);stroke-width:1}
.axis{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--muted);font-size:10px}
.axlabel{fill:var(--ink2);font-size:10.5px}
.ptlabel{fill:var(--ink);font-size:10px}
.ta-end{text-anchor:end}.ta-mid{text-anchor:middle}
.pt{cursor:pointer}.pt:hover{stroke:var(--ink);stroke-width:1.5}
.view{border-left:3px solid var(--s1)}
.viewhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.headline{font-size:17px;font-weight:650;margin:8px 0 10px;line-height:1.45}
.bullets,.watch{margin:0;padding-left:18px}
.bullets li{margin:7px 0}
.watch li{margin:5px 0;color:var(--ink2)}
.watchhead{font-size:12px;color:var(--muted);margin:12px 0 4px;text-transform:uppercase;
 letter-spacing:.06em}
.badge{font-size:11px;border:1px solid var(--ring);border-radius:20px;padding:2px 9px;color:var(--ink2)}
.conf-low{border-color:var(--bad);color:var(--bad)}
.conf-high{border-color:var(--good);color:var(--good)}
.badge.warn{border-color:var(--s2);color:var(--s2)}
.banner{background:var(--surface);border:1px solid var(--s2);border-left:3px solid var(--s2);
 border-radius:8px;padding:10px 14px;margin:12px 0;font-size:12.5px;color:var(--ink2)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(136px,1fr));gap:8px;margin:14px 0}
.kpi{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:11px 13px}
.kl{color:var(--muted);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kv{font-size:21px;font-weight:640;margin:3px 0 1px;letter-spacing:-.02em}
.kd{font-size:11.5px;color:var(--muted)}
.d-chg,.d-na{color:var(--ink2)}
.sechead{display:flex;align-items:baseline;gap:10px;margin:30px 0 6px;flex-wrap:wrap}
.sechead h3{font-size:16px;margin:0;letter-spacing:-.01em}
.ssub{color:var(--muted);font-size:12px}
.lchip{font-size:11px;font-weight:650;letter-spacing:.08em;color:var(--surface);
 background:var(--ink);border-radius:4px;padding:2px 8px}
.klayer{font-size:10px;letter-spacing:.08em;color:var(--muted);margin-bottom:3px}
.kpi{cursor:help}
.khint{font-size:10.5px;color:var(--muted);line-height:1.4;margin:1px 0 4px}
.blay{display:inline-block;font-size:10px;letter-spacing:.06em;color:var(--surface);
 background:var(--ink2);border-radius:3px;padding:1px 6px;margin-right:7px;
 vertical-align:1px;white-space:nowrap}
.explain{font-size:12px;color:var(--ink2);line-height:1.65;margin:10px 0 2px;
 border-left:2px solid var(--ring);padding:2px 0 2px 12px}
.explain p{margin:0 0 6px}
.spec{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:9px 0 0;
 font-size:11.5px;line-height:1.55;padding:8px 0 0;border-top:1px dashed var(--ring)}
.spec .sk{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.spec .sv{color:var(--ink2)}
.spec .sw{color:var(--s2)}
.smgrid{display:grid;gap:8px;margin:10px 0 2px}
@media(max-width:760px){.smgrid{grid-template-columns:repeat(2,1fr)!important}}
.sm{border:1px solid var(--ring);border-radius:8px;padding:2px 4px 0;background:var(--surface);
 cursor:help}
.sm:hover{border-color:var(--ink2)}
.smname{fill:var(--ink);font-size:9.5px;font-weight:640}
.smval{fill:var(--muted);font-size:9px;font-variant-numeric:tabular-nums}
.specwarn{font-size:11.5px;line-height:1.55;color:var(--ink2);margin:6px 0 0}
.specwarn b{color:var(--s2);font-weight:640;margin-right:7px}
.explain b{color:var(--ink);font-weight:640}
.chip{cursor:help}
.asidenote{color:var(--muted);font-size:11.5px;margin:-6px 0 12px;padding-left:2px}
.panel strong{font-weight:640;color:var(--ink2)}
nav{position:sticky;top:0;z-index:8;display:flex;gap:2px;flex-wrap:wrap;
 background:color-mix(in srgb,var(--plane) 88%%,transparent);backdrop-filter:blur(8px);
 border-bottom:1px solid var(--ring);margin:0 -16px 4px;padding:8px 16px}
nav a{font-size:12.5px;color:var(--ink2);text-decoration:none;padding:5px 11px;
 border-radius:7px;white-space:nowrap}
nav a:hover{background:var(--surface);color:var(--ink)}
nav a.on{background:var(--ink);color:var(--surface)}
.block{margin:18px 0 4px;scroll-margin-top:52px}
.blkhead{display:flex;align-items:baseline;gap:10px;width:100%%;background:none;
 border:0;border-bottom:1px solid var(--ring);padding:8px 2px;cursor:pointer;
 text-align:left;flex-wrap:wrap;font:inherit;color:inherit}
.blkhead:hover{border-color:var(--ink2)}
.blabel{font-size:11px;font-weight:650;letter-spacing:.10em;color:var(--surface);
 background:var(--ink);border-radius:4px;padding:3px 9px}
.btitle{font-size:16px;font-weight:640;letter-spacing:-.01em;color:var(--ink)}
.bsub{font-size:11.5px;color:var(--muted)}
.bsignal{font-size:12px;line-height:1.6;color:var(--ink2);margin:8px 0 2px;
 background:var(--surface);border:1px solid var(--ring);border-left:3px solid var(--ink2);
 border-radius:0 7px 7px 0;padding:8px 12px}
.bsignal b{color:var(--ink);font-weight:640;margin-right:8px}
.bchev{margin-left:auto;color:var(--muted);font-size:12px;transition:transform .15s}
.block.fold .bchev{transform:rotate(-90deg)}
.block.fold .blkbody{display:none}
.blkbody{padding-top:2px}
.tight{border-left:3px solid var(--s2)}
.tigrid{display:flex;gap:22px;align-items:center;margin:12px 0 4px;flex-wrap:wrap}
.tiscore{display:flex;align-items:baseline;gap:4px}
.tiv{font-size:40px;font-weight:660;letter-spacing:-.03em;line-height:1}
.tim{color:var(--muted);font-size:13px}
.tibar{flex:1;min-width:240px;position:relative}
.tibar::before{content:"";display:block;height:8px;border-radius:4px;
 background:linear-gradient(90deg,var(--s3),var(--grid) 50%%,var(--s2))}
.tifill{position:absolute;top:-3px;width:0;height:14px;border-right:2px solid var(--ink)}
.titicks{display:flex;justify-content:space-between;color:var(--muted);font-size:10.5px;
 margin-top:4px}
.tistat{color:var(--ink2);font-size:12px;margin-top:6px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 2px}
.chip{border:1px solid var(--ring);border-radius:7px;padding:5px 9px;font-size:11.5px;
 display:flex;gap:7px;align-items:baseline;background:var(--plane)}
.chip .cl{color:var(--muted);font-size:10px}
.chip .cn{color:var(--ink2)}
.chip .cv{font-variant-numeric:tabular-nums;color:var(--ink)}
.chip.c-t{border-color:var(--s2)}
.chip.c-e{border-color:var(--s3)}
.sq1{fill:var(--seq1)}.sq2{fill:var(--seq2)}.sq3{fill:var(--seq3)}
.legend{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px;
 color:var(--ink2);margin:-6px 0 14px;padding-left:18px}
.sw{width:11px;height:11px;border-radius:3px;display:inline-block;margin-left:10px}
.sw.s1{background:var(--s1)}.sw.s2{background:var(--s2)}.sw.s3{background:var(--s3)}
.sw.s4{background:var(--s4)}.sw.s5{background:var(--s5)}.sw.s6{background:var(--s6)}
.sw.s7{background:var(--s7)}.sw.s8{background:var(--s8)}
.endlab{font-size:9.5px;font-weight:640}
.lgnote{color:var(--muted);margin-left:14px}
[hidden]{display:none!important}
.freqbar{display:flex;align-items:center;gap:6px;flex-wrap:wrap;
 margin:0 0 8px;padding-left:18px}
.fq{font:inherit;font-size:12px;line-height:1;cursor:pointer;
 padding:5px 11px;border-radius:6px;color:var(--ink2);
 background:var(--plane);border:1px solid var(--grid)}
.fq:hover{color:var(--ink);border-color:var(--baseline)}
.fq.on{background:var(--s1);border-color:var(--s1);color:#fff;font-weight:600}
.fqnote{font-size:11.5px;color:var(--muted);margin-left:8px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:720px){.two{grid-template-columns:1fr}}
.empty{color:var(--muted);font-size:12.5px;padding:22px;text-align:center;
 border:1px dashed var(--ring);border-radius:8px}
.ph{color:var(--muted);font-size:13px;padding:34px;text-align:center;
 border:1px dashed var(--ring);border-radius:8px;letter-spacing:.04em}
.tbl{margin-top:10px;overflow-x:auto}
.tbl summary{cursor:pointer;font-size:12px;color:var(--ink2);user-select:none}
table{border-collapse:collapse;width:100%%;font-size:11.5px;margin-top:8px;
 font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--ring);padding:4px 7px;text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--surface);
 padding:5px 9px;border-radius:6px;font-size:11.5px;opacity:0;transition:opacity .08s;
 z-index:9;max-width:320px}
footer{color:var(--muted);font-size:11.5px;margin-top:26px;line-height:1.7}
code{background:var(--plane);padding:1px 5px;border-radius:4px;font-size:12px}
"""

JS = """
var tip=document.getElementById('tip');
document.addEventListener('mouseover',function(e){
  var t=e.target.closest('[data-tip]'); if(!t)return;
  tip.textContent=t.getAttribute('data-tip'); tip.style.opacity=1;});
document.addEventListener('mousemove',function(e){
  if(tip.style.opacity!='1')return;
  var x=e.clientX+13,y=e.clientY+13,r=tip.getBoundingClientRect();
  if(x+r.width>innerWidth)x=e.clientX-r.width-13;
  if(y+r.height>innerHeight)y=e.clientY-r.height-13;
  tip.style.left=x+'px'; tip.style.top=y+'px';});
document.addEventListener('mouseout',function(e){
  if(e.target.closest('[data-tip]'))tip.style.opacity=0;});
/* crosshair for dense line charts: too many points to give each one a marker,
   so snap to the nearest x and report every series at that date */
document.querySelectorAll('rect.hit').forEach(function(hit){
  var d=JSON.parse(hit.getAttribute('data-series'));
  var svg=hit.ownerSVGElement, cross=svg.querySelector('line.cross');
  hit.addEventListener('mousemove',function(ev){
    var r=svg.getBoundingClientRect(), vb=svg.viewBox.baseVal;
    var ux=(ev.clientX-r.left)/r.width*vb.width;
    var i=Math.round((ux-d.ml)/d.pw*(d.x.length-1));
    if(i<0)i=0; if(i>d.x.length-1)i=d.x.length-1;
    var cx=d.ml+d.pw*i/(d.x.length-1);
    cross.setAttribute('x1',cx); cross.setAttribute('x2',cx); cross.setAttribute('opacity','0.45');
    var lines=[d.x[i]];
    d.s.forEach(function(sr){ if(sr.f[i]!=null) lines.push(sr.n+': '+sr.f[i]); });
    if(d.a&&d.a[i]) lines.push(d.a[i]);
    tip.textContent=lines.join('  ·  '); tip.style.opacity=1;
    var x=ev.clientX+13,y=ev.clientY+13,tb=tip.getBoundingClientRect();
    if(x+tb.width>innerWidth)x=ev.clientX-tb.width-13;
    tip.style.left=x+'px'; tip.style.top=y+'px';});
  hit.addEventListener('mouseleave',function(){
    cross.setAttribute('opacity','0'); tip.style.opacity=0;});
});
/* fold blocks, remembered across reloads */
document.querySelectorAll('.block').forEach(function(b){
  var id=b.getAttribute('data-block'), h=b.querySelector('.blkhead');
  try{ if(localStorage.getItem('dcw-fold-'+id)==='1'){b.classList.add('fold');
       h.setAttribute('aria-expanded','false');} }catch(_){}
  h.addEventListener('click',function(){
    var f=b.classList.toggle('fold');
    h.setAttribute('aria-expanded', f?'false':'true');
    try{localStorage.setItem('dcw-fold-'+id, f?'1':'0')}catch(_){}});
});
/* highlight the nav entry for whichever block is in view */
(function(){
  var links=[].slice.call(document.querySelectorAll('nav a[data-nav]'));
  var blocks=[].slice.call(document.querySelectorAll('.block'));
  if(!links.length||!blocks.length)return;
  var nav=document.querySelector('nav'), raf=0;
  function pick(){
    raf=0;
    var edge=(nav?nav.getBoundingClientRect().height:0)+24, cur=blocks[0];
    blocks.forEach(function(b){
      if(b.getBoundingClientRect().top<=edge)cur=b;   // last one past the nav
    });
    /* the final block is short enough that its top never clears the nav, so
       once the page is scrolled to the bottom, select it explicitly */
    if(innerHeight+scrollY>=document.body.scrollHeight-4)cur=blocks[blocks.length-1];
    var id=cur.getAttribute('data-block');
    links.forEach(function(a){a.classList.toggle('on',a.getAttribute('data-nav')===id);});
  }
  addEventListener('scroll',function(){if(!raf)raf=requestAnimationFrame(pick);},{passive:true});
  addEventListener('resize',function(){if(!raf)raf=requestAnimationFrame(pick);});
  pick();
})();
/* frequency toggle. Panels sharing a data-freqgroup switch together, so the
   level chart and its growth chart are never showing different frequencies —
   which would be worse than no toggle at all. Every bar in the group stays in
   sync, so either panel can drive. */
(function(){
  var groups={};
  document.querySelectorAll('[data-freqgroup]').forEach(function(el){
    var g=el.getAttribute('data-freqgroup');
    (groups[g]=groups[g]||[]).push(el);
  });
  Object.keys(groups).forEach(function(g){
    var bars=[],views=[];
    groups[g].forEach(function(r){
      [].push.apply(bars,r.querySelectorAll('.freqbar'));
      [].push.apply(views,r.querySelectorAll('[data-freq]'));
    });
    if(!bars.length||!views.length)return;
    var key='dcw-freq-'+g;
    var valid={};
    bars.forEach(function(b){b.querySelectorAll('.fq').forEach(function(btn){
      valid[btn.getAttribute('data-set')]=1;});});
    function apply(v){
      if(!valid[v])return false;
      bars.forEach(function(b){b.querySelectorAll('.fq').forEach(function(btn){
        var on=btn.getAttribute('data-set')===v;
        btn.classList.toggle('on',on);
        btn.setAttribute('aria-pressed',on?'true':'false');});});
      views.forEach(function(e){e.hidden=e.getAttribute('data-freq')!==v;});
      try{localStorage.setItem(key,v)}catch(_){}
      return true;
    }
    bars.forEach(function(b){b.addEventListener('click',function(e){
      var btn=e.target.closest('.fq'); if(btn)apply(btn.getAttribute('data-set'));});});
    var sv=null; try{sv=localStorage.getItem(key)}catch(_){}
    if(!(sv&&apply(sv)))apply('d');
  });
})();
var btn=document.getElementById('tt');
function cur(){var s=document.documentElement.getAttribute('data-theme');
  if(s)return s;
  return matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light';}
function sync(){btn.textContent=cur()==='dark'?'\\u2600 \\u6d45\\u8272':'\\u263e \\u6df1\\u8272';}
btn.addEventListener('click',function(){
  var n=cur()==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',n);
  try{localStorage.setItem('dcw-theme',n)}catch(_){} sync();});
try{var sv=localStorage.getItem('dcw-theme');
  if(sv)document.documentElement.setAttribute('data-theme',sv);}catch(_){}
sync();
"""

PAGE = u"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Data Center Watch · 每日看板</title>
<style>%(css)s</style>
</head><body class="viz"><div class="wrap">
<header>
  <h1>Data Center Watch</h1>
  <span class="meta">数据截至 %(asof)s · 累计 %(obs)d 次观测 · 生成于 %(built)s</span>
  <button id="tt" type="button">主题</button>
</header>
<nav>%(nav)s</nav>
%(body)s
<footer>
Source: OpenRouter (openrouter.ai/rankings), as of %(asof)s · 吞吐与延迟 OpenRouter endpoint stats ·
GPU 租赁价与可用率 Shadeform（~18 家 neocloud）· 存储现货 TrendForce。<br>
Token 数由各上游厂商自己的 tokenizer 统计，跨厂商不可直接比较。
OpenRouter 不含第一方 API 与云托管端点流量，为开发者 / agentic coding 切片，非市场总量。
支出为列表价估算，不含企业折扣与缓存定价，属上限估计。
GPU 口径为 neocloud 跨云中位数，与超大规模云合约价不可直接比较。<br>
本页为自建研究工具，不构成投资建议。
</footer>
</div><div id="tip"></div><script>%(js)s</script></body></html>
"""


def render_css():
    """Substitute the palette's light/dark values into the stylesheet."""
    return CSS % {("%s_%s" % (k, s)): PAL[k][i]
                  for k in PAL for i, s in enumerate(("l", "d"))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./data")
    ap.add_argument("--state", default="./state")
    ap.add_argument("--out", default="./docs")
    ap.add_argument("--window", default="week", choices=["day", "week", "month"])
    ap.add_argument("--memory-absolute", action="store_true",
                    help="publish TrendForce absolute quotes instead of indexed series")
    a = ap.parse_args()

    html = build(a.data, a.state, a.out, a.window, a.memory_absolute)
    os.makedirs(a.out, exist_ok=True)
    p = os.path.join(a.out, "index.html")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(html)
    print("site -> %s  (%.0f KB, self-contained)" % (p, len(html.encode("utf-8")) / 1024.0))


if __name__ == "__main__":
    main()
