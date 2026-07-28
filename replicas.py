# -*- coding: utf-8 -*-
"""How many GPUs one replica of a model actually occupies.

The break-even concurrency metric normalises per GPU-equivalent, which quietly
assumes a model fits on one card. It does not: a 350B-parameter MoE needs eight
H100s just to hold its weights, so its true break-even is roughly eight times
the per-GPU figure. That is the difference between "needs 82 concurrent streams"
and "needs 656" — the first looks comfortable, the second does not.

Two inputs, both real rather than assumed:

  parameter count   HuggingFace `safetensors.total`, joined via the `hf_slug`
                    that OpenRouter carries on each model.
  serving precision OpenRouter's per-endpoint `quantization` field — the
                    precision providers actually serve at, not the precision the
                    weights were released in. GLM was published in BF16 but is
                    served FP8 by most providers, a 2x difference in footprint.

Only open-weight models can be estimated. Claude, GPT and Gemini publish no
parameter count, so they are reported as unknown rather than guessed at.

    python replicas.py --data ./data
"""
import argparse, csv, io, json, math, os, time, urllib.request

HF = "https://huggingface.co/api/models/%s?expand[]=safetensors"
UA = "Mozilla/5.0 (compatible; dc-watch/1.0)"
TIMEOUT = 35

# Bytes per parameter by serving precision.
QUANT_BYTES = {
    "fp4": 0.5, "mxfp4": 0.5, "nf4": 0.5, "int4": 0.5, "q4": 0.5,
    "fp6": 0.75,
    "fp8": 1.0, "int8": 1.0, "q8": 1.0,
    "fp16": 2.0, "bf16": 2.0, "float16": 2.0,
    "fp32": 4.0, "float32": 4.0,
}
DEFAULT_QUANT = "fp8"          # observed mode across providers

# An 80 GB H100 cannot give all 80 GB to weights: KV cache, activations and
# fragmentation take a large share, and more so at long context. 0.60 is a
# deliberately round assumption, surfaced on the page rather than buried.
VRAM_GB = 80.0
USABLE_FRAC = 0.60


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


def hf_params(slug, retries=2):
    """HF rate-limits aggressively; back off rather than dropping the model."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(HF % slug, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            sf = d.get("safetensors") or {}
            return sf.get("total"), (sf.get("parameters") or {})
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(2.5 * (attempt + 1))
    raise last


def gpus_for(params, quant, vram_gb=VRAM_GB, usable=USABLE_FRAC):
    bpp = QUANT_BYTES.get((quant or "").lower(), QUANT_BYTES[DEFAULT_QUANT])
    weight_gb = params * bpp / (1024.0 ** 3)
    need = weight_gb / (vram_gb * usable)
    # real deployments use power-of-two tensor-parallel groups
    n = max(1, int(2 ** math.ceil(math.log(max(1.0, need), 2))))
    return weight_gb, need, min(n, 64), bpp


def collect(datadir, delay=1.8, vram_gb=VRAM_GB, usable=USABLE_FRAC):
    perf = read_csv(os.path.join(datadir, "model_perf.csv"))
    if not perf:
        print("  no model_perf.csv - run perf.py first")
        return []
    latest = max(r["run_date"] for r in perf)
    cur = {}
    for r in perf:
        if r["run_date"] == latest:
            cur[(r["model"], r.get("variant", ""))] = r

    # cache across runs: parameter counts effectively never change
    cache_path = os.path.join(datadir, "hf_params_cache.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(io.open(cache_path, encoding="utf-8"))
        except ValueError:
            cache = {}

    out, fetched = [], 0
    for (model, variant), r in cur.items():
        slug = (r.get("hf_slug") or "").strip()
        quant = (r.get("quantization") or "").strip() or DEFAULT_QUANT
        rec = {"run_date": latest, "model": model, "variant": variant,
               "hf_slug": slug, "quantization": quant}
        if not slug:
            rec.update({"params": "", "weight_gb": "", "gpus_raw": "",
                        "gpus_per_replica": "", "bytes_per_param": "",
                        "status": "closed_weights"})
            out.append(rec)
            continue
        if slug in cache:
            total = cache[slug]
        else:
            try:
                total, _ = hf_params(slug)
                fetched += 1
            except Exception as e:
                print("  ! %-34s %s" % (slug[:34], str(e)[:50]))
                rec.update({"params": "", "weight_gb": "", "gpus_raw": "",
                            "gpus_per_replica": "", "bytes_per_param": "",
                            "status": "hf_error"})
                out.append(rec)
                time.sleep(delay)
                continue
            cache[slug] = total
            time.sleep(delay)
        if not total:
            rec.update({"params": "", "weight_gb": "", "gpus_raw": "",
                        "gpus_per_replica": "", "bytes_per_param": "",
                        "status": "no_safetensors"})
            out.append(rec)
            continue
        wgb, need, n, bpp = gpus_for(float(total), quant, vram_gb, usable)
        rec.update({"params": int(total), "weight_gb": round(wgb, 1),
                    "gpus_raw": round(need, 2), "gpus_per_replica": n,
                    "bytes_per_param": bpp, "status": "ok"})
        out.append(rec)

    json.dump(cache, io.open(cache_path, "w", encoding="utf-8"))
    if out:
        from tracker import append_csv
        append_csv(os.path.join(datadir, "model_replicas.csv"),
                   list(out[0].keys()), out)
    print("  fetched %d new param count(s), %d cached" % (fetched, len(cache) - fetched))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./data")
    ap.add_argument("--vram", type=float, default=VRAM_GB)
    ap.add_argument("--usable", type=float, default=USABLE_FRAC,
                    help="fraction of VRAM available to weights after KV cache")
    a = ap.parse_args()
    rows = collect(a.data, vram_gb=a.vram, usable=a.usable)
    ok = [r for r in rows if r["status"] == "ok"]
    print()
    print("  %-34s %-6s %9s %9s %6s" % ("model", "quant", "参数", "权重GB", "卡数"))
    for r in sorted(ok, key=lambda x: -x["gpus_per_replica"]):
        print("  %-34s %-6s %8.0fB %9.1f %6d"
              % (r["model"].split("/")[-1][:34], r["quantization"],
                 r["params"] / 1e9, r["weight_gb"], r["gpus_per_replica"]))
    closed = [r for r in rows if r["status"] == "closed_weights"]
    if closed:
        print()
        print("  闭源无参数量（%d 个）: %s"
              % (len(closed), ", ".join(r["model"].split("/")[-1][:22] for r in closed[:6])))
    print()
    print("  假设：单卡 %.0f GB，权重可用 %.0f%%（其余留给 KV cache 与激活）；"
          % (a.vram, 100 * a.usable))
    print("  卡数向上取到 2 的幂，因为张量并行组通常是 1/2/4/8。")


if __name__ == "__main__":
    main()
