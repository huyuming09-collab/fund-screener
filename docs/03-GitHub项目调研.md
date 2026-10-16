# GitHub 类似项目调研

调研时间：2025年。目的：为基金评分工具寻找可借鉴的数据源方案、指标计算实现、架构模式。

## 值得借鉴的项目

### 1. [piginzoo/fund_analysis](https://github.com/piginzoo/fund_analysis)
个人基金分析项目（Python）。**借鉴点**：
- 从天天基金爬净值的 API 方案（`fund.eastmoney.com/f10/F10DataApi.aspx?type=lsjz&code=...`），支持分页、日期区间、增量续爬——我们的数据层可直接复用该思路；
- 基金列表来源：`fund.eastmoney.com/js/fundcode_search.js`，一次性拿到全部基金代码+名称+类型；
- 夏普比率分年/季/月/周多周期计算、无风险利率用1年期国债收益率；
- 使用 SQLite 存储（Python 内置，零运维），符合我们"轻量、可迁移"的选型。

### 2. [Euzohn/ZFundPilot](https://github.com/Euzohn/ZFundPilot)
自托管中国基金组合分析工具（FastAPI + SQLite + Pandas + React）。**借鉴点**：
- 数据源策略：**AkShare 优先、天天基金兜底**的双源容灾——直接采纳；
- "组合体检"四维评分（配置/风险/流动性/收益，每维0-100 + 综合定级）——与我们分维度评分思路一致，佐证了方案可行性；
- 后端 FastAPI + 前端分离的架构，正好是微信小程序需要的 API 层形态；
- 基金筛选器、风险阈值配置化（`RiskThresholds` dataclass）的做法。

### 3. [lusongno1/Fund-Selection](https://github.com/lusongno1/Fund-Selection)
爬取基金数据辅助散户选基的项目。**借鉴点**：选基维度的白名单参考。

### 4. [jichenstudy/FundLens](https://github.com/jichenstudy/FundLens)
Python + LLM 的基金分析工具（筛选对比、定投回测、估值）。**借鉴点**：定投回测、风险评估报告的输出格式。

### 5. 数据源库：[AKShare](https://github.com/akfamily/akshare)
免费开源金融数据接口，覆盖天天基金/晨星等口径的基金净值、经理、持仓、费率数据。**结论**：
本项目数据层第一版直接用 AkShare（`fund_open_fund_info_em` 净值、`fund_scale_cn_em` 规模等），避免自己写爬虫的反爬维护成本。

## 调研结论（对本项目的三点影响）

1. **不做新的爬虫轮子**：数据层 = AkShare 主源 + 天天基金 API 兜底（复用项目1/2验证过的接口）；
2. **开源项目普遍缺少"科学评分方法论"**：多数只做筛选和指标罗列，没有分类型、分维度、带红线和回测验证的评分体系——这正是本项目的核心差异化（详见 `01-债券基金评分指南.md`）；
3. **架构定型**：后端 Python（Pandas 计算 + SQLite 快照 + FastAPI 出 API），小程序只做展示层，评分逻辑全部在后端，保证"隔段时间重评"可以后台定时跑。
