# -*- coding: utf-8 -*-
"""Have Opus 5 write the daily view from the change brief.

Reads state/brief.json plus the last few days of its own output (so it can
build on, rather than repeat, what it already said) and appends a structured
entry to state/commentary.jsonl.

The relay at ANTHROPIC_BASE_URL is LAN-only; this step cannot run in cloud CI.
Opus 5 returns thinking blocks ahead of text, so text is extracted by block
type rather than by index.

    python commentary.py --dry-run     # print the prompt, call nothing
    python commentary.py               # call Opus 5 and append the entry
"""
import argparse, datetime as dt, io, json, os, re, sys

MODEL = "claude-opus-5"
RECENT_TO_SHOW = 3

SYSTEM = u"""你是一位硬件与 AI 基础设施方向的卖方研究分析师，为一个每日看板写「今日变动速报」。
读者是专业投资人，天天看这个页面，方法论他早就懂了。

# 输出要求：报变动，不解释

- **headline**：一句话，20 字以内，必须含一个数字。
- **bullets**：**必须按下面这个固定顺序**，每层最多一条，每条 20-45 字：

  | 顺序 | layer 字段值 | 该层看什么 |
  |---|---|---|
  | 1 | `需求` | token 用量、请求数、应用榜（谁在烧 token） |
  | 2 | `价格与收入` | vwap、列表均价、est_spend、量与钱的份额 |
  | 3 | `成本` | GPU 租赁价、可用率、超大规模云挂牌价、DRAM/NAND |
  | 4 | `单位经济` | 盈亏平衡并发（每副本优先） |
  | 5 | `资本市场` | 股票各环节涨跌、距 52 周高点 |

  某一层今天确实没有值得报的变动，就**省略该条**，不要写「无变化」凑数。
  **资本市场必须合并成一条**，不许拆成「光模块…」「存储…」「ASIC…」三条。
- **watch**：0 到 2 条，每条 30 字以内。只写「需要下一次数据确认的信号」，不写泛泛风险。
- 用中文。数字必须来自 brief，缺失就说缺失，绝不编造。

## 严格禁止（这些是本条速报被判为失败的条件）

1. **禁止解释口径和方法论。** 不要写「这是因为窗口不同」「该口径不含企业折扣」
   「OpenRouter 不含第一方 API」之类。这些说明已经永久印在页面各面板下方，
   每天重复一遍就是噪音。
2. **禁止复述你前几天说过的常识性提醒。** 观测数少、噪声大这类话，
   confidence 字段已经承载了，正文里不要再写。
3. **禁止一条 bullet 里塞两三个论点。** 一条一个事实。写不完就删掉最不重要的。
4. **禁止形容词堆砌。**「显著」「大幅」「持续」一律删掉，只留数字。

## 对照示例

坏（193 字，混了解释和口径）：
「周度口径 VWAP 环比 -6.73% 至 0.9249 美元/百万 token，同期无权重列表均价反升 3.73%
至 4.0393 美元，两者倍差扩至约 4.4 倍。列表均价被 440 个在售模型中的高价长尾拉动，
只反映挂牌分布；VWAP 才是真实成交单位经济，二者反向不构成矛盾。」

好（31 字，且带 layer）：
{"layer": "价格与收入", "text": "VWAP 环比 -6.73% 至 $0.925/Mn，同期列表均价反升 3.73%。"}

资本市场合并示例（一条讲完全部板块，不拆条）：
{"layer": "资本市场", "text": "8 个板块单日全跌，光模块 -5.3% 最深，服务器是唯一月内正收益 +4.4%。"}

# 背景知识（用来防止你算错，但绝对不要写进输出）

- avg_list_price 是挂牌价无权重均值，被高价长尾拉高；vwap 才是实际成交价。两者差 3-6 倍属正常，不是矛盾。
- vwap 的分母是 paid_tokens 而非 total_tokens（后者含免费档）。spend / paid_tokens 恒等于 vwap。
- est_spend 按挂牌价算，是上限。
- day 窗口与 week 窗口不可接续比较。
- DRAM 日更、NAND 周更。
- gpu_market.value 是跨云中位数；availability_pct 是价格的领先指标（先零可用、后涨价）。
- hyperscaler 三档（按需/LowPri/Spot）差约 5 倍，不可混用。
- bridge 看 break_even_streams_replica（每副本）而非 break_even_streams（每 GPU）；
  前者中位约 200 路时意味着贴近盈亏线。闭源模型无副本数。

调用 publish_view 工具输出，五个字段全部必填。"""


def load_env(path=".env"):
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


def read_recent(path, n):
    if not os.path.exists(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out[-n:]


def trim_brief(brief):
    """Drop the long history arrays — the model needs levels and deltas, not
    every datapoint. Keeps the prompt small and the signal dense."""
    b = json.loads(json.dumps(brief))
    for m in (b.get("llm", {}).get("metrics") or {}).values():
        m.pop("history", None)
    for m in ((b.get("llm", {}).get("daily") or {}).get("metrics") or {}).values():
        m.pop("history", None)
    for m in (b.get("memory") or {}).values():
        m.pop("history", None)
        m.pop("indexed", None)
    for m in (b.get("gpu", {}).get("gpus") or {}).values():
        m.pop("history", None)
    models = b.get("llm", {}).get("models") or {}
    for k in ("top20_volume", "top20_spend"):
        if k in models:
            models[k] = models[k][:10]

    # The newer layers are verbose relative to their signal. Trimming them keeps
    # the prompt dense: a 35 KB brief dilutes attention across sections that a
    # one-line verdict will never mention.
    for m in (b.get("gpu_market") or {}).values():
        m.pop("history", None)
        m.pop("availability_history", None)
    for m in (b.get("hyperscaler") or {}).values():
        m.pop("history", None)
    br = b.get("bridge") or {}
    if br.get("models"):
        # only the extremes matter for a verdict; drop the middle
        ms = sorted(br["models"], key=lambda x: x["break_even_streams"])
        br["models"] = ms[:5] + ms[-5:] if len(ms) > 10 else ms
        br.pop("note", None)
    ap = b.get("apps") or {}
    if ap.get("available"):
        ap["overall"] = (ap.get("overall") or [])[:8]
        ap["trending"] = (ap.get("trending") or [])[:5]
        ap["coding"] = (ap.get("coding") or [])[:5]
        ap["by_subcategory"] = {k: v[:3] for k, v in
                                (ap.get("by_subcategory") or {}).items() if v}
    eq = b.get("equity") or {}
    if eq.get("available"):
        eq.pop("names", None)        # group aggregates carry the signal
        eq.pop("note", None)
    return b


def build_prompt(brief, recent):
    b = trim_brief(brief)
    meta = b.get("llm", {}).get("meta", {})
    parts = [
        u"# 今日变化简报（机器生成，数值均来自一手 API）",
        u"",
        u"历史长度：%d 次观测，最早 %s。数据缺口：%s。采集降级：%s。"
        % (meta.get("observations", 0), meta.get("first_run", "n/a"),
           (u"%d 天" % len(meta.get("gaps") or [])) if meta.get("gaps") else u"无",
           u"是 - " + meta.get("degrade_note", "") if meta.get("degraded") else u"否"),
        u"",
        u"```json",
        json.dumps(b, ensure_ascii=False, indent=1),
        u"```",
    ]
    if recent:
        parts += [u"", u"# 你最近几天写过的摘要（不要重复这些论点，要在其上推进或修正）", u""]
        for e in recent:
            parts.append(u"## %s" % e.get("date", "?"))
            parts.append(u"- headline: %s" % e.get("headline", ""))
            # two bullets is enough to prevent repetition; the older entries are
            # long-form and would otherwise dominate the prompt
            for x in (e.get("bullets") or [])[:2]:
                parts.append(u"  - %s" % bullet_text(x)[:80])
    parts += [u"", u"调用 publish_view 工具输出今天的观点。"]
    return u"\n".join(parts)


def extract_json(text):
    """Only used as a last resort if the tool-use path is unavailable. The
    model writes Chinese prose containing quotes and brackets, which regularly
    breaks free-form JSON, so the tool path below is strongly preferred."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        return None


# Structured output via forced tool use. Hand-parsing the model's JSON failed
# in practice — Chinese prose with embedded quotes produced unescaped strings.
# The tool path returns an already-parsed dict, so no string parsing at all.
TOOL = {
    "name": "publish_view",
    "description": "Publish today's analyst view to the dashboard. Every field is mandatory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": u"一句话，20 字以内，必须含一个数字。不解释原因。",
            },
            "bullets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "layer": {
                            "type": "string",
                            "enum": ["需求", "价格与收入", "成本", "单位经济", "资本市场"],
                            "description": u"该条属于哪一层。必须按 需求→价格与收入→成本→单位经济→资本市场 的顺序排列。",
                        },
                        "text": {
                            "type": "string",
                            "description": u"20-45 字，一条一个事实：指标+数值+变化方向。禁止解释口径。",
                        },
                    },
                    "required": ["layer", "text"],
                },
                "description": u"按固定层序排列，每层最多一条，无变动的层省略。资本市场必须合并为一条。",
            },
            "watch": {
                "type": "array",
                "items": {"type": "string"},
                "description": u"0 到 2 条，每条 30 字以内。只写需要下一次数据确认的具体信号，不写泛泛风险。",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": u"必填。对今日结论的置信度。观测次数少于 10 次时应为 low。",
            },
            "confidence_reason": {
                "type": "string",
                "description": u"必填。一句话说明置信度理由，通常与历史长度、数据缺口或采集降级有关。",
            },
        },
        "required": ["headline", "bullets", "watch", "confidence", "confidence_reason"],
    },
}
REQUIRED = TOOL["input_schema"]["required"]

LAYER_ORDER = [u"需求", u"价格与收入", u"成本",
               u"单位经济", u"资本市场"]


def bullet_text(b):
    """Entries written before bullets carried a layer are plain strings."""
    return b if isinstance(b, str) else (b.get("text") or "")


def bullet_layer(b):
    return "" if isinstance(b, str) else (b.get("layer") or "")


def normalise_bullets(bs):
    """Sort into the canonical layer order and collapse any layer the model
    split across several bullets — capital markets is the usual offender."""
    out, seen = [], {}
    for b in bs or []:
        if isinstance(b, str):
            out.append({"layer": "", "text": b})
            continue
        lay, txt = (b.get("layer") or ""), (b.get("text") or "").strip()
        if not txt:
            continue
        if lay in seen:
            seen[lay]["text"] = (seen[lay]["text"].rstrip(u"。.") + u"；" + txt)
        else:
            seen[lay] = {"layer": lay, "text": txt}
            out.append(seen[lay])
    rank = {l: i for i, l in enumerate(LAYER_ORDER)}
    return sorted(out, key=lambda b: rank.get(b["layer"], 99))


def call_opus(prompt, observations=0, max_tokens=3000, retries=1):
    """Returns (view_dict, usage). Uses forced tool use for structure."""
    import anthropic
    base = os.environ.get("ANTHROPIC_BASE_URL")
    key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("no ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY available")
    client = anthropic.Anthropic(base_url=base, api_key=key, timeout=180)

    view, usage = {}, {"input_tokens": 0, "output_tokens": 0}
    msg = prompt
    for attempt in range(retries + 1):
        r = client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=SYSTEM,
            tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
            messages=[{"role": "user", "content": msg}])
        usage["input_tokens"] += r.usage.input_tokens
        usage["output_tokens"] += r.usage.output_tokens
        blocks = [b for b in r.content if b.type == "tool_use"]
        if blocks:
            view = dict(blocks[0].input or {})
        else:
            # relay returned prose instead of a tool call
            text = "".join(b.text for b in r.content if b.type == "text")
            view = extract_json(text) or {}

        missing = [k for k in REQUIRED if not view.get(k)]
        if view.get("bullets") and not any(
                (bullet_text(b) or "").strip() for b in view["bullets"]):
            missing.append("bullets")
        if not missing:
            return view, usage
        if attempt < retries:
            msg = prompt + (u"\n\n上一次调用漏了这些必填字段：%s。请重新调用 publish_view，"
                            u"补齐全部字段。" % ", ".join(missing))

    # The relay does not strictly enforce `required`. Rather than fail the whole
    # run over a missing confidence label, derive it from history length — that
    # is the dominant driver anyway — and say so.
    if not view.get("confidence"):
        view["confidence"] = ("low" if observations < 10
                              else "medium" if observations < 30 else "high")
        view["confidence_reason"] = view.get("confidence_reason") or (
            u"模型未返回置信度，按历史长度自动判定（当前 %d 次观测）。" % observations)
    if not view.get("headline"):
        raise ValueError("Opus 5 returned no headline; refusing to publish an empty view")
    return view, usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", default="./state/brief.json")
    ap.add_argument("--out", default="./state/commentary.jsonl")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt and exit without calling the model")
    a = ap.parse_args()
    load_env()

    if not os.path.exists(a.brief):
        print("no brief at %s - run analyze.py first" % a.brief)
        return 1
    with io.open(a.brief, encoding="utf-8") as f:
        brief = json.load(f)

    recent = read_recent(a.out, RECENT_TO_SHOW)
    prompt = build_prompt(brief, recent)

    if a.dry_run:
        print("=" * 70)
        print("SYSTEM (%d chars)" % len(SYSTEM))
        print("=" * 70)
        print(SYSTEM)
        print()
        print("=" * 70)
        print("USER (%d chars, ~%d tokens)" % (len(prompt), len(prompt) // 2))
        print("=" * 70)
        print(prompt[:4000])
        if len(prompt) > 4000:
            print("... [%d more chars]" % (len(prompt) - 4000))
        return 0

    meta = brief.get("llm", {}).get("meta", {})
    parsed, usage = call_opus(prompt, observations=meta.get("observations", 0))
    entry = {
        "date": dt.date.today().isoformat(),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        "as_of": meta.get("as_of", ""),
        "observations": meta.get("observations", 0),
        "headline": parsed.get("headline", ""),
        "bullets": normalise_bullets(parsed.get("bullets")),
        "watch": parsed.get("watch", []),
        "confidence": parsed.get("confidence", ""),
        "confidence_reason": parsed.get("confidence_reason", ""),
        "usage": usage,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with io.open(a.out, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print("commentary appended -> %s" % a.out)
    print("  tokens in=%d out=%d | confidence=%s"
          % (usage["input_tokens"], usage["output_tokens"], entry["confidence"]))
    print()
    print("  " + entry["headline"])
    for x in entry["bullets"]:
        lay = bullet_layer(x)
        print("   - [%s] %s" % (lay, bullet_text(x)) if lay else "   - " + bullet_text(x))
    for x in entry["watch"]:
        print("   ! " + x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
