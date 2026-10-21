# AGENTS.md — 本仓库的工作规矩

> 这里只写**长期有效**的硬规矩。运营层面"当前暂时不提什么、暂不发什么榜"属于阶段性决策，
> 不在本文件，见本机 `运营/00-总览/当前发布策略.md`（该目录已 gitignore，以用户口头为准）。
> 想知道"现在什么状态"看 [STATUS.md](STATUS.md)；想知道"文件放哪"看 [docs/00-项目结构与约定.md](docs/00-项目结构与约定.md)。

---

## 1. 两条主线，永不混装

| | A 线 · 仓库（入库） | B 线 · 本机运营（永不入库） |
|---|---|---|
| 内容 | 工具代码、网页、结论级文档、状态账本 | 选题、草稿、成品文案、配图、素材、台账 |
| 路径 | `fundscorer/` `web/` `tests/` `docs/` `scripts/` `README.md` | `运营/`（含其全部子目录） |
| 原则 | 只放"别人复现工具所需"的一切 | 只放"做内容生产所需"的一切，且含未发布信息 |

**判定口诀**：这段内容删掉后，别人还能不能把工具跑起来、把网页看懂？
- 能 → 只进本地；不能 → 进仓库。
- 拿不准 → 问用户，不要自作主张入库。**`运营/` 下任何文件都不许 add、commit、push。**

## 2. 改动前置

改任何代码前先取基线，改完必须复现：

```bash
.venv/bin/python -m pytest tests/ -q     # 现基线：271 passed
node tests/smoke_page.js                  # 现基线：SMOKE PASS
```

- 触碰 `pipeline.py` / `cards.py` / `data.py` → 重跑 `timeout 900 .venv/bin/python -m fundscorer.pipeline --skip-refresh`（约 1.3 min）后再验
- 触碰 `web/index.html` → 必跑 `smoke_page.js`；导出模式改动需实际打开 `?export=<pool>&size=xhs` 目检
- 管线超 60s 会被移到后台作业，**用 job_output 收结果，不要重复起任务**

## 3. 提交与推送

- commit message 中文，前缀 `feat` / `fix` / `docs` / `chore` / `refactor`，正文写清"改了什么 + 为什么 + 验证结果"
- **push 前确认暂存区不含 `运营/` 路径**：`git diff --cached --name-only | grep '^运营/'` 必须为空
- 本机到 GitHub 常年 TLS 抖动（`GnuTLS recv error -110`、连接超时），**失败直接重试即可**，不要改 remote、不要换协议；`weekly_update.sh` 已内置 3 次重试
- 大改分小步提交：一次提交只做一件事，便于回退与审阅

## 4. 口径与合规（长期有效）

1. **反例脱敏**：任何负面举例不得出现真实公司名，用「头部机构 / 老牌机构 / 大厂 / 中型机构 + 产品特征」代替；正面榜单按公开数据如实写产品名
2. **波动口径统一**：一律写「近5年年化波动」；样本必须**净值≥5年且历史完整**。历史不足时指标会退化为全期计算（字段名仍是 `*_5y`），**引用任何 `*_5y` 字段前必须配 `nav_years` 闸**，否则数字会被当成满5年
3. **分数口径**：所有分数标注为「池内相对评分」；**不同池的分数不可横向比较**（各池独立标准化），凡出现分数必须带这句说明
4. **文末免责**：对外内容固定附「历史业绩不代表未来，不构成投资建议」
5. **数字来源**：一律取当期产物（`data/top20.json` / `web/data.js` / `data/metrics_cache.pkl`），**不手抄旧文**；发布前逐个数字回核

## 5. 配图规范

- 社媒配图**只导出榜单图**：`web/index.html?export=<pool>&size=xhs|wide`（`<pool>` = `short_bond` / `long_bond` / `fixed_plus`）
- 全分类工作台：`?export=all`
- **不要再新增封面 / 对照 / 直方图 / 表格类导出模式**——这类图不随周更维护，会与当期数据脱节；确有需要先问用户
- 页面当前只有 `EXPORT_SIZES.xhs` 与 `.wide` 两档，导出图为一次性快照，**不进周更管线**

## 6. 状态同步纪律

**新增/删除功能、改动任何开关、调整路线图 → 同一个提交内更新 [STATUS.md](STATUS.md)。**

- 开关状态（`config.INDEX["enabled"]`、`CARD.categories[].hidden`）变动必须写进 STATUS 第 2 节并注明"为什么关着 / 什么条件打开"
- 周更后更新 STATUS 第 3 节"数据基准"与第 6 节"最近变更"
- 已知问题解决或新增，同步第 4 节

## 7. 禁止事项

- 不把当期数据、榜单、公司名、具体产品判断**写死**进 `web/index.html` 或任何源码（一律读 `web/data.js`）
- 不改 `.gitignore` 中 `运营/` 的忽略规则；不在 `运营/` 下建"给仓库看"的文件
- 不删 `data/metrics_cache.pkl`（6,901 只的池指标缓存，删了要全量重抓）
- 不改 `scripts/weekly_update.sh` 的 staged 路径守卫与"只 add web/data.js"逻辑
- 不改评分权重 / 筛选阈值 / 红线参数而不告知用户（`config.py` 全部参数改动都属产品决策）
- 不引入新的第三方依赖（`requirements.txt` 现仅 pandas/numpy/pyarrow，标准库优先）

## 8. 环境

- Python 虚拟环境：`.venv/`（**不要**用系统 python；本机曾出现 conda 污染导致缺依赖）
- 数据抓取仅在**运行日**做：平时跑管线一律 `--skip-refresh`，避免触发基金公司限频
- 周更由 crontab 每周五 23:00 调 `scripts/weekly_update.sh`：管线 → 素材包（本地）→ 守卫 → 提交推送
