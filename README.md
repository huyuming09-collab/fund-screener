# 基金选择指南 · 分类型基金评分框架

基金评分后端 + **静态网页周榜**（原计划微信小程序，因小程序对投研/荐基类内容有合规限制，改为普通网页展示）。**先做债券类**（纯债型 / 固收+），后续扩展到指数、混合、股票等全类型。
核心不是"罗列指标"，而是**一套经过方法论论证、可回测验证、能定期自动重评**的评分体系。

> ⚠️ 本项目输出为历史数据分析结论，**不构成投资建议**。

---

## 快速开始

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt

# 给一批基金评分（自动分类分池 + 同池比较 + 红线 + 星级）
.venv/bin/python -m fundscorer 110017 006980 009689 160323 --json out.json

# ★ 周更管线（每周五 23:00 crontab 自动执行）：刷新数据 → 筛选 → 筛后评分 → 取前20 → 导出
.venv/bin/python -m fundscorer.pipeline                  # 完整周更
.venv/bin/python -m fundscorer.pipeline --skip-refresh   # 不抓数据，仅用现有缓存重筛重评（调试）

# 短债两段式筛选（手动模式，读取 data/full_scores.json 快照）
.venv/bin/python -m fundscorer.screen --csv data/screen_report.csv [--top 20]

# 运行单元测试
.venv/bin/python -m pytest tests/ -q
```

示例输出（4 只参考基金实测）：

```
代码      名称              池                  总分    星级
009689  易方达瑞锦混合A        固收+池             83.0  ★★★★
160323  华夏磐泰混合(LOF)A    固收+池             74.0   ★★★
110017  易方达增强回报债券A      固收+池             60.7   ★★★
006980  国寿安保泰恒纯债债券      纯债型·中长期纯债池       59.2    ★★
```

注：示例池仅 4 只，属**小样本**，分位分仅供参考。正式评分需全市场同类池（见路线图）。

---

## 方法论

评分标准的完整论证在 [`docs/01-债券基金评分指南.md`](docs/01-债券基金评分指南.md)（V5.0，经多轮评审迭代）。
要点：

1. **先分类分池，再同类比较**（R0）：纯债型（短债池 / 中长期池）与固收+ 分开打分，
   两套评分卡权重不同——纯债稳定性占 30 分，固收+ 收益占 25 分。
2. **主窗口 5 年**：覆盖 2020 利率上行、2022-11 赎回潮、2024-09 股债切换多轮压力期，
   避免 1~2 年数据的偶然性。
3. **长期性价比指数 LVI**：`超额收益 ÷ (0.6×最大回撤 + 0.4×下行波动)`，
   量化"用更小的风险换更高的收益"，是本框架的核心排序指标。
4. **经理全产品历史年化**：不只看本基金，看现任经理**职业生涯全部产品**的任职年化中位数。
5. **排名稳定性五层机制**：慢窗口 / 缩尾分位 / 滑动中位 / 星级滞回 / 事件观察期，
   防止"这月前 1%、下月后 1%"的排名抖动。
6. **五条红线**：新经理、迷你规模、异常回撤、疑似踩雷、深回撤未修复 → 强制封顶 60 分。

GitHub 同类项目调研见 [`docs/03-GitHub项目调研.md`](docs/03-GitHub项目调研.md)。
现有开源项目多为"指标罗列/筛选器"，缺少分类评分方法论与回测验证——这是本项目的差异化。

**短债筛选**（`fundscorer/screen.py`，标准见 [`docs/07-短债筛选标准.md`](docs/07-短债筛选标准.md)）：
先筛后评两段式——份额合并（ABCD 合并计规模，基金级口径：经理任期/从业取同经理全份额最大值，
业绩取历史最久份额）→ 硬条件（规模≥5亿 / 经理从业≥5年 / 现经理任本基金≥3年 /
成立≥5年 / 近5年无单日涨幅>1% 异常）→ 性能条件（四项分位均值进池内前55% + 年度收益分位中位≥55%，
保证 ≥20 支）→ 按 A 类份额的 v5.2 全市场分数降序输出。

---

## 代码结构

```
fundscorer/
  __init__.py     score_funds() 顶层入口
  __main__.py     CLI
  config.py       权重/阈值/红线/星级/筛选参数（改权重不用动代码；文件头有"参数索引"）
  data.py         数据层：天天基金 pingzhongdata(复权净值)、F10、经理、评级、中债指数 + 本地缓存
  metrics.py      指标：年化/回撤/波动/修复/LVI/缩尾分位/夏普/阶段收益（纯计算，无 IO）
  scorer.py       分类 → 指标采集 → 同池打分 → 红线 → 星级 → 快照（score_only 支持只评子集）
  rank.py         全市场批量评分：入池 → 全市场分数 → metrics_cache + full_scores.json 快照
  screen.py       短债两段式筛选：份额合并 → 硬条件 S1~S5 → 性能条件 P1~P2 → 按 A 类分数排序
  batch.py        批量抓取/持久化：nav_full 固化、池指标强制重算（refresh_pool_metrics）
  pipeline.py     ★ 周更管线：刷新 → 筛选 → 筛后评分 → TOP20 → CSV/top20.json/data.js 原子导出
  cards.py        卡片数据契约：榜单 → 详情字段 + 净值曲线采样 → JSON（top20.json / web/data.js）
  backtest.py     历史回测与因素分析
tests/            单元测试（不依赖网络，合成数据验证口径；含 score_only 等价性测试）
docs/             评分指南 V5、回测分析、短债筛选标准（07）、周更与卡片（08）
web/              周榜网页：index.html（响应式：手机满宽 / 电脑 480px 窄栏居中）+ data.js（pipeline 生成）
scripts/          weekly_update.sh（周更 + 自动 git push，crontab 调用）
.github/workflows/  deploy-pages.yml（push 即把 web/ 部署到 GitHub Pages）
data/             运行数据（**不入 git**，见 .gitignore）：cache/、nav_full/、metrics_cache.pkl、
                  full_scores.json、screen_report.csv、top20.json、logs/、pipeline_*.log
                  例外入库：fund_company_map.csv + company_scale_nonmoney.csv（外部采集、代码不可再生成）
```

### GitHub 仓库与线上部署

- 公开仓库：`https://github.com/huyuming09-collab/fund-screener`（MIT 许可）
- 线上页面（GitHub Pages，由 `.github/workflows/deploy-pages.yml` 自动部署 `web/`）：
  `https://huyuming09-collab.github.io/fund-screener/`
- 数据流：本机 crontab 每周五跑 `scripts/weekly_update.sh` → 管线导出新 `web/data.js`
  → 自动 commit/push → Pages 工作流重新部署 → 线上榜单刷新。管线失败则不推送，线上保留上一期。
- 克隆后首次运行：`pip install -r requirements.txt` 后直接 `python -m fundscorer.pipeline`；
  `data/cache`、`nav_full` 等会自动重建（公司规模映射两个 CSV 已随仓库提供）。

### 参数在哪改（`fundscorer/config.py` 文件头有完整索引）

| 要改什么 | 改哪里 |
|---|---|
| 各池打分权重 | `POOL_WEIGHTS` |
| 短债筛选条件（规模/任期/分位阈值） | `SCREENS["short_bond"]`（未来 `long_bond`/`fixed_plus`/`index` 同 key 扩展） |
| 打分指标权重外的阈值（回撤容忍等） | `RISK_THRESHOLD` / `REDLINES` |
| 星级配额与门槛 | `STARS` |
| 榜单条数 | `TOP_N`（默认 20） |
| 夏普无风险利率（展示用，不计分） | `SHARPE_RF`（默认 1.5%） |
| 卡片窗口/分类 tab | `CARD` |
| 周更处理哪些池 / 日志目录 | `PIPELINE` |

### 周更流程（每周五 23:00 自动）

```
crontab: 30 23 * * 5  →  flock + scripts/weekly_update.sh  （日志 data/pipeline_cron.log）
  1. build_universe → crawl（nav/f10/inst 抓取）→ persist_nav（nav_full 固化）
  2. refresh_pool_metrics：短债池指标强制重算（绕过 collect_all 陈旧缓存复用）
  3. load_screen_inputs → screen_candidates（硬条件+性能条件漏斗）
  4. score_pool(全池分位基准, score_only=候选份额) —— 只对筛后基金打分，分数与全市场快照逐位一致
  5. rank_candidates(top_n=TOP_N=20)
  6. 原子导出：data/screen_report.csv（内部含 star 列）+ data/top20.json（卡片契约，无星级字段）
     + web/data.js（周榜网页数据）；漏斗/耗时/失败落 data/pipeline_YYYYMMDD.log
  7. weekly_update.sh：data.js 有变化 → git commit + push → GitHub Pages 自动重新部署
```

网页周榜：双击打开 `web/index.html`（file:// 直接可用，手机/电脑自适应，页面底部常显免责声明）。

### 数据源与容灾

| 数据 | 主源 | 说明 |
|---|---|---|
| 复权净值 | 天天基金 `pingzhongdata` | **必须用复权口径**，高分红债基用单位净值会严重失真（110017 两者年化差 3.7pp） |
| F10 概况 | 天天基金 F10 | 类型/规模/费率/经理/基准 |
| 经理从业/产品 | 天天基金经理页 + akshare `fund_manager_em` | 全产品任职年化中位数 |
| 晨星评级 | akshare `fund_rating_all` | |
| 基准指数 | akshare 中债新综合财富指数 | |

---

## 路线图

- [x] **全市场同类池**：批量抓取全部债券基金入池，分位分与星级配额生效
- [x] **每周五自动重评**：crontab → `fundscorer.pipeline`（刷新 → 筛选 → 筛后评分 → TOP20 → 导出），见 docs/08
- [x] **周榜网页**：web/index.html 响应式页面（榜单卡 + 支付宝风详情 + 常显免责声明；手机满宽 / 电脑窄栏居中），数据契约见 docs/08
- [x] ~~**FastAPI 服务层**：为微信小程序提供 REST 接口~~（已取消：小程序停做，网页直读 `data.js` 无需服务层）
- [ ] **稳定性回测**：验证"高星基金未来收益更高、回撤更小"（Spearman > 0.3）与"排名不剧烈跳变"
- [ ] **扩展基金类型**：指数基金、混合基金、股票基金各自的评分卡（复用 `config.py` + `scorer.py` 骨架）
- [ ] 费率分 A/C/E 份额类别比较；基金公司非货规模接入真实数据源
- [ ] 经理资质调节（R3）、星级滞回（L4）、滑动中位（L3）的完整实现

---

## 免责声明

评分基于公开历史数据（天天基金 / AKShare），可能存在延迟与错误。
历史高分不代表未来收益，本工具不构成任何投资建议。
