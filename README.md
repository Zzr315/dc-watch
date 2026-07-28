# Data Center Watch — LLM → Token → 算力 全链条日度看板

每天自动采集，算出变化，让 Opus 5 写一段观点，渲染成一个本地网页。

覆盖七层：**Token 需求 → Token 定价 → 服务吞吐 → GPU 租赁价 → GPU 供给 → 存储成本 → 单位经济**。
起点是复刻 J.P. Morgan《Data Center Watch》的三条数据链，现已扩展到它没有的四层。

---

## 快速开始

```bash
cp .env.example .env        # 填 ANTHROPIC_AUTH_TOKEN（观点摘要用）
python run_daily.py         # 跑一次全链条，产出 docs/index.html
```

直接用浏览器打开 `docs/index.html` 即可。**单文件、零外部请求**，双击就能看，也能直接发给别人。

注册每日任务（19:15 触发）：

```powershell
powershell -ExecutionPolicy Bypass -File setup_task.ps1
```

> **为什么是 19:15**：TrendForce 的 DRAM 现货 18:10 (GMT+8) 才收盘。早上跑只能拿到前一天的价。

---

## 七层数据链

| 层 | 内容 | 来源 | 频率 | 鉴权 |
|---|---|---|---|---|
| 1 需求 | Token 用量、请求数、in/out 拆分 | OpenRouter `frontend/v1/rankings/models` | 日 | 免 |
| 1b 归因 | **谁在烧 token**（Cursor / Cline / CLI agent） | OpenRouter `v1/datasets/app-rankings` | 日 | **需免费 key** |
| 2 价格 | 全模型价目表、三种价格口径 | OpenRouter `v1/models` | 日 | 免 |
| 3 吞吐 | 各 provider tok/s、延迟、请求数 | OpenRouter `frontend/v1/stats/endpoint` | 日 | 免 |
| 4 算力价 | 10 种 GPU 跨 ~18 家 neocloud 中位价 | Shadeform `v1/instances/types` | 日 | 免 |
| 4b 超大规模云 | H100/A100/MI300X 按需·LowPri·Spot 挂牌价 | Azure `retail/prices` | 日 | 免 |
| 5 供给 | 各 GPU 有货区域占比 | 同上 | 日 | 免 |
| 6 成本 | DRAM 现货 / NAND 现货 | TrendForce | 日 / **周** | 免 |
| 7 单位经济 | 盈亏平衡并发（每 GPU + **每副本**） | 本地计算 + HF 参数量 | 日 | 免 |
| 8 资本市场 | 13 只 AI 基础设施标的，按产业链环节分组 | Yahoo chart API | 日 | 免 |
| 9 合成 | AI 基础设施紧张度扩散指数 | 本地计算 | 日 | — |

**除层 1b 外全部免费免鉴权。** Silicon Data 的付费 GPU 指数是可选增强，设 `SD_TOKEN` 后与 Shadeform 并列显示以交叉校验。

### 关于 404K 那张图

网传的 404K Semi-Ai 看板，**逐面板对应 Silicon Data 的 API**：代币支出=`token-index?token=expenditure`、
五个 GPU=`gpu-index` 的五个合法值、新型云/超大规模云=`index_version=neo/hs`、
GDDR6=`ram-index`（它只支持 gddr6 一种）、远期曲线=`forward-curve`。它是 Silicon Data 的封装转售。

我们的免费源与其读数对比：**A100 80G 精确命中（1.65 vs 1.65）、H100 差 4%**（2.60 vs 2.71），
B200 +14%、H200 +36%，MI300X 未覆盖。

---

## 三个自建指标

### 盈亏平衡并发（层 7，全网没有）

```
单流每小时创收 = tok/s × 3600 × 输出单价
盈亏平衡并发   = GPU 时租 ÷ 单流每小时创收
```

一张 H100 要同时跑多少路请求，token 收入才够付租金。

**两个口径都要看：**

| 口径 | 中位 | 含义 |
|---|---|---|
| 每 GPU 等效 | ~5 路 | 假设模型跑在一张卡上——**不成立** |
| **每副本** | **~205 路** | 用真实参数量与服务精度折算后的数 |

后者才是有意义的。参数量取自 HuggingFace `safetensors`，**服务精度取自 OpenRouter 各 provider
的 `quantization` 字段**（实测以 fp8 为主，而非发布时的 bf16——差 2 倍显存），
单卡 80GB、权重可用 60%，卡数向上取到 2 的幂。

修正后中位落到 205 路，**正好在真实服务并发区间（20–200）内**——意味着这些低价开源模型
在挂牌价下贴近盈亏线，而非每 GPU 口径显示的宽裕。实测：

```
MiMo v2.5     311B fp8  8卡   每GPU  78 路 → 每副本 625 路
DeepSeek Flash 158B fp8  4卡   每GPU  54 路 → 每副本 218 路
GLM 5.2       753B fp8 16卡   每GPU   4 路 → 每副本  65 路   ← 定价足够高，有真实毛利
```

闭源模型（Claude / GPT / Gemini）不公开参数量，只有每 GPU 数，标注为「闭源未知」。

**刻意做成反解。** 若改算「每 token 成本」就必须假设 batch size——那是不可观测的量，
且会主导结果，正是让卖方支出估算失真的同一类隐藏假设。

### GPU 三个价格档次不能混用（层 4/4b）

| 档 | H100 实测 | 说明 |
|---|---|---|
| neocloud 中位 | $2.60 | ~18 家新型云，JPM/404K 的「新型云」口径 |
| Azure Spot | $2.95 | 可抢占，落在 neocloud 附近 |
| Azure LowPri | $4.90 | 可抢占容量；经验上框住已公开的「合约价」指数 |
| **Azure 按需挂牌** | **$15.98** | 唯一可引用的真实序列，是 neocloud 的 **6.1 倍** |

企业协议价介于三者之间，**任何地方都不公开**。引用「超大规模云价格」时必须说清是哪一档，
否则数字可以差 5 倍。

### GPU 供给紧张度（层 5，JPM 没有）

有货区域占比。实测 B200/B300 全线 0%、H100 14%、RTX4090 50%——
新卡通常**先零可用、后涨价**，所以它是价格的领先指标。

### 三种价格口径同图（修正 JPM 的盲区）

| 口径 | 含义 |
|---|---|
| 无权重列表均价 | ~300 个在售模型价目表的算术平均，被 o1-pro（$375/Mn）这类长尾拉高，反映**挂牌分布** |
| VWAP | 按真实 prompt/completion 拆分算出的**实际成交单位经济** |
| 中点加权（未采用） | 用 (输入+输出)/2 会高估约 2.7 倍，因为实际流量约 29:1 输入重 |

原报告只用 "simplified assumptions" 一句带过，但这个假设撑起了它整个支出量级。

---

## 文件

```
run_daily.py       每日入口（Task Scheduler 调这个）
  tracker.py       层 1/2/6：OpenRouter 量价 + TrendForce 存储 + 原始价目表存档
  apps.py          层 1b：应用级需求归因（需免费 key）
  shadeform.py     层 4/5：neocloud GPU 租赁价 + 可用率
  azure.py         层 4b：超大规模云挂牌价（按需/LowPri/Spot）
  perf.py          层 3：Top-N 模型的 tok/s、延迟、服务精度、hf_slug
  replicas.py      层 7 输入：每副本 GPU 数（HF 参数量 × 服务精度）
  equity.py        层 8：13 只 AI 基础设施标的
  analyze.py       变化率、异动、缺口、桥接、扩散指数 → state/brief.json
  commentary.py    Opus 5 观点摘要 → state/commentary.jsonl
  build_site.py    渲染 docs/index.html
  publish.py       可选：安全闸 + git push（默认关闭）
backfill.py        一次性回补历史用量
make_charts.py     出 PNG 放 PPT 用，不在每日链路
setup_task.ps1     注册 / 移除计划任务（默认 08:00 与 20:00 两次）
看板.bat           双击打开看板
```

所有采集器统一走 `tracker.append_csv` 写盘——它会在字段增减时重写表头并对齐历史行。
早期各采集器自带裸 `DictWriter`，加字段那次直接把 `model_perf.csv` 写成了 ragged。

常用：

```bash
python run_daily.py --only site      # 只用现有数据重渲染
python run_daily.py --perf-top 30    # 多抓几个模型的吞吐
python shadeform.py                  # 单独看当前 GPU 价与可用率
python analyze.py                    # 只重算 brief
```

每步独立 try/except：LLM 中转挂了不影响当天采集，页面退回上一版点评并标注「点评未更新」。
日志在 `state/run.log`。

---

## 密钥

| 变量 | 必需性 |
|---|---|
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | 观点摘要需要。内网中转，云端 CI 不可达，这是必须本地跑的原因 |
| `OPENROUTER_API_KEY` | 层 1b（应用归因）与 `backfill.py` 需要。**免费可建，零余额已实测可用**，无需充值 |
| `SD_TOKEN` | 可选，Silicon Data 付费 GPU 指数 |

Task Scheduler 不继承交互式 shell 的环境变量，所以密钥必须写进 `.env`。

---

## 冷启动：哪些能补、哪些不能

| 序列 | 能否回补 |
|---|---|
| Token **用量**历史 | ✅ 至 2025-01-01（`backfill.py`，需免费 key） |
| Token **价格/支出** | ❌ 无历史 API，从第一天累积 |
| GPU 价/可用率 | ❌ 只有实时快照，从第一天累积 |
| 存储现货 | ❌ 免费页只给当日值 |

| 能力 | 需要观测数 |
|---|---|
| 日环比 | 2 |
| 异动检测（>2σ） | 11 |
| 月环比 | 30 |
| 同比 | 365 |

---

## 已知限制

- **OpenRouter ≠ 市场总量。** 不含第一方 API 与 Bedrock/Vertex 流量，是开发者 / agentic coding 切片。
- **token 跨厂商不可加总。** 各家用自己的 tokenizer 统计，官方明示。
- **支出是上限估计。** 列表价口径，不含企业折扣、批量定价、缓存折扣。
- **GPU 是 neocloud 口径。** 跨云中位数，与超大规模云合约价不可直接比。实测 H100 与 JPM 所用 Silicon Data 差约 4%，A100/B200 偏高约 20%——追变化率可靠，绝对水平需标注。
- **两个未文档化端点**（`frontend/v1/rankings/models`、`frontend/v1/stats/endpoint`）已迁移过一次。前者失败自动降级到文档化的 `rankings-daily`（丢 in/out 拆分），页面挂降级横幅。
- **机器关机 = 当天数据永久缺口**，页面画断点而非直线连接。
- **存储绝对报价不上公开页。** TrendForce 是商业数据商，默认显示指数化序列与变化率；要绝对值用 `build_site.py --memory-absolute`。

---

## 若要发布到公网

默认不发布。`publish.py` 有两道防线（`.gitignore` + 提交前独立扫描暂存区，
命中禁止模式则重置暂存区并退出码 2）。启用：

```bash
python run_daily.py --publish
```

需先建 GitHub 仓库并把 Pages 指向 `main` 分支 `/docs`。
注意：JPM 的 PDF 与其页面渲染图受版权保护且禁止再分发，已被 `.gitignore` 排除。
