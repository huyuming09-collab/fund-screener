# -*- coding: utf-8 -*-
"""基金评分器全局配置。评分方法论见 docs/01-债券基金评分指南.md (V5.2)。

参数索引（要改什么去哪改，改这里不用动逻辑代码）：
  改权重          → PURE_BOND_WEIGHTS / POOL_WEIGHTS（各池评分卡，100 分制）
  改筛选条件      → SCREENS["short_bond"/"long_bond"]（S1~S5 硬条件 + P1~P2 性能条件 + 排序）
  改打分标准      → QUALITY（绝对分档）/ RISK_THRESHOLD（风险门槛给分）/ REDLINES（红线封顶）
                    STARS（星级阈值与配额）/ HISTORY（数据不足判定）
  改 TOP 取数     → TOP_N（周更输出前 N 支）
  改夏普无风险利率 → SHARPE_RF（仅展示，不计分）
  改卡片展示窗口  → CARD（阶段收益窗口 / 走势采样 / 字段定义）
  改周更范围      → PIPELINE（刷新哪个池 / 日志目录）
  改数据清洗      → NAV_GLITCH_RET / GLITCH_MIN_POINTS
"""

MODEL_VERSION = "v5.3"

# ---------------------------------------------------------------- 窗口
WINDOWS = {"primary_years": 5, "secondary_years": 3, "aux_years": 1}

# ---------------------------------------------------------------- 纯债型评分卡（100 分）
# v5.1 权重 = 回测混合方案 B（docs/05-历史回测因素分析.md §6）：
#   风险块/稳定性/经理块保留固定配额（回测证实风险因子无排序信息量，角色是尾部排除
#   与跨周期保险，不参与 IC 加权）；其余 61 分按 2019H1~2022H1 训练段方向对齐 IC 分配，
#   单因子上限 15。组合层验证：前20%持有1年胜率 现行0.565 → 混合B 0.601（验证段三池全部提升）。
# 下表为短债池分配（fee/ret_1y/winrate 按短债池训练段 IC）；中长期纯债用 PURE_BOND_WEIGHTS_LONG。
PURE_BOND_WEIGHTS = {
    # 风险块 15（固定；门槛式打分，只排除尾部不做排序——IC为负系债牛状态依赖）
    "mdd_5y": 5, "mdd_3y": 3, "mdd_1y": 2, "repair_5y": 4, "vol_5y": 1,
    # IC 池 61：低费率+近端动量被证实最强
    "fee": 8.91, "ret_3y": 8.65, "ret_5y": 8.46, "ret_1y": 7.69, "yearly_5y": 7.49,
    "q_excess_20q": 7.10, "lvi_5y": 6.22, "lvi_3y": 6.02, "winrate_m_5y": 0.46, "age": 0,
    # 固定块：稳定性 3 + 经理 20（任期4+从业年限5+全产品年化11）+ 晨星 1
    "rank_stab": 3, "mgr_tenure": 4, "mgr_career": 5, "mgr_products_ann": 11,
    "ms_rating": 1, "inst_ratio": 0, "fund_scale": 0, "company_scale": 0,
}

# 中长期纯债池（同结构，IC 池内按该池训练段 IC 分配；winrate IC 为正故权重高于短债）
PURE_BOND_WEIGHTS_LONG = {
    "mdd_5y": 5, "mdd_3y": 3, "mdd_1y": 2, "repair_5y": 4, "vol_5y": 1,
    "ret_1y": 9.97, "ret_3y": 8.82, "lvi_3y": 8.27, "yearly_5y": 7.29,
    "q_excess_20q": 6.92, "ret_5y": 6.48, "lvi_5y": 5.49, "winrate_m_5y": 4.25,
    "fee": 3.51, "age": 0,
    "rank_stab": 3, "mgr_tenure": 4, "mgr_career": 5, "mgr_products_ann": 11,
    "ms_rating": 1, "inst_ratio": 0, "fund_scale": 0, "company_scale": 0,
}

# ---------------------------------------------------------------- 固收+评分卡（100 分）
# v5.2：目标函数回测（docs/06，收益+回撤预注册决策选出 calmar 目标 IC 权重）——
# v5.1 押 24 分动量（ret_1y）被证实"多赚抵不上多担回撤"（验证段 u2 −1.26%→+0.86%），
# 因子对"未来平稳"的预测力远强于对"未来收益"（mdd_1y→未来回撤 IC +0.72）。
# v2 试验（2026-10-18 外部评审采纳）："纯收益为 0"修正为"风险调整收益 25 分"——
# 卡玛 15 + 夏普 10（分子分母结合，直接对应"风险预算内卡玛最优"）；启用 inst_ratio
# 2 分（>90% 机构定制盘记 0）。纯收益/纯超额族仍为 0（不追历史绝对收益）。
FIXED_PLUS_WEIGHTS = {
    # 风险 35（回撤阶梯 + 波动门槛；原 44 分等比压缩）
    "mdd_1y": 10, "mdd_3y": 8, "mdd_5y": 6, "vol_5y": 11, "repair_5y": 0,
    # 风险调整收益 25：calmar=ret_5y/mdd_5y、sharpe=(ret_5y−rf)/vol_5y，池内分位
    "calmar_5y": 15, "sharpe_5y": 10,
    # 胜率与运营 26（原 IC 池 44 压缩重排：胜率 17 + 费率 6 + 任期 3）
    "winrate_m_5y": 9, "q_win_20q": 8, "fee": 6, "mgr_tenure": 3,
    # 机构占比 2（20%~90% 满分、>90% 定制盘记 0）
    "inst_ratio": 2,
    # 固定块 12：不可回溯（面板无经理全产品/晨星字段）沿用原值
    "ret_1y": 0, "age": 0, "lvi_3y": 0, "lvi_5y": 0, "ret_5y": 0, "excess_5y": 0,
    "ret_3y": 0, "yearly_5y": 0, "q_excess_20q": 0,
    "rank_stab": 0, "mgr_career": 0, "mgr_products_ann": 11, "ms_rating": 1,
    "fund_scale": 0, "company_scale": 0,
}

# 池 → 权重表（v5.1 起短债与中长期纯债分表）
POOL_WEIGHTS = {
    "short_bond": PURE_BOND_WEIGHTS,
    "long_bond": PURE_BOND_WEIGHTS_LONG,
    "fixed_plus": FIXED_PLUS_WEIGHTS,
}

# 需要在同类池内算百分位的指标（正向=越大越好；逆向=越小越好）
PERCENTILE_METRICS = {
    "mdd_5y": "lower", "mdd_3y": "lower", "mdd_1y": "lower",
    "vol_5y": "lower", "winrate_m_5y": "higher", "repair_5y": "lower",
    "ret_5y": "higher", "ret_3y": "higher", "ret_1y": "higher",
    "excess_5y": "higher", "lvi_5y": "higher", "lvi_3y": "higher",
    "q_excess_20q": "higher", "q_win_20q": "higher", "rank_stab": "lower",
    "mgr_products_ann": "higher",   # 经理全产品年化：同池经理间比较
    "calmar_5y": "higher", "sharpe_5y": "higher",   # 风险调整收益（v2 试验，由 ret/mdd/vol 派生）
}

# 质地维度绝对分档参数
QUALITY = {
    "age_full_years": 5, "age_mid_years": 3,
    "mgr_tenure_full_years": 5, "mgr_tenure_mid_years": 3,
    "mgr_career_full_years": 8, "mgr_career_mid_years": 5, "mgr_career_min_years": 3,
    "fee_benefit_top_pct": 0.10, "fee_penalty_bottom_pct": 0.10,
    "ms_rating_map": {5: 1.0, 4: 0.8, 3: 0.6, 2: 0.4, 1: 0.2},
    "inst_ratio_full": (0.20, 0.90),   # 20%~90% 满分
    "inst_ratio_mid": (0.10, 0.20),
    # 基金规模 U 型（亿元）：(下限, 上限, 得分比例)
    "fund_scale_u": [(0.0, 0.5, 0.0), (0.5, 2.0, 0.4), (2.0, 10.0, 0.75),
                     (10.0, 100.0, 1.0), (100.0, 500.0, 0.85), (500.0, 1e12, 0.65)],
    # 基金公司非货规模（亿元）分档
    "company_scale_map": [(8000, 1.0), (3000, 0.8), (1000, 0.6), (300, 0.4), (0, 0.2)],
}

# ---------------------------------------------------------------- LVI 与回撤修复口径
LVI_RC_WEIGHTS = {"rc_w_mdd": 0.6, "rc_w_dvol": 0.4}   # 风险代价 = 0.6×最大回撤 + 0.4×下行波动
LVI_RC_FLOOR = 0.002   # 风险代价下限(0.2%)；理财债回撤~0.01%(rc≈0.006%)时 LVI 分母爆炸，
                       # 取中性分位；正常短债 rc 常 0.3%~1%，不受影响

# ---------------------------------------------------------------- 基金公司规模排除（用户口径）
# 基金公司非货规模 < 门槛（亿元）直接从全市场剔除，不评分不入池（"小公司风险大"）。
# 数据源：东财总规模 − 货币型季末规模（sources/company_scale.py，与官方口径中位误差0.30%）。
# 无公司映射或无规模数据 → 保留（数据不足不冤枉排除）。
COMPANY_SCALE = {
    "exclude_below_yi": 300,          # <300亿 排除（用户选定；163家中排除69家）
    "map_path": "data/fund_company_map.csv",
    "scale_path": "data/company_scale_nonmoney.csv",
}

# ---------------------------------------------------------------- 纯债风险"门槛给满分"（用户口径）
# 纯债的回撤/波动只要"够稳"就该给满分，不再用纯百分位过度区分（否则长城短债0.57%波动
# 仍因"有人更低"被打到19分，把收益97.5分全吃掉）。≤池均值×full_at_mult 给满分，
# 超出后线性扣分，到 zero_at_mult×池均值 归零。
RISK_THRESHOLD = {
    "full_at_mult": 1.5,     # ≤ 池均值×1.5 → 满分（用户：持续比平均好50%以内都算稳）
    "zero_at_mult": 4.0,     # ≥ 池均值×4.0 → 0分
    "min_pool": 20,          # 池样本太少时退回分位法
}

REPAIR = {
    # 显著回撤绝对下限（按池）
    "sig_dd_floor": {"short_bond": 0.0015, "long_bond": 0.005, "fixed_plus": 0.03},
    # 得分分档（T 占子项满分比例）
    "bands_pure": [(15, 1.0), (30, 0.85), (60, 0.7), (120, 0.4), (10**9, 0.0)],
    "bands_plus": [(30, 1.0), (60, 0.85), (120, 0.7), (240, 0.5), (10**9, 0.0)],
    "age_discount_cap": 3,   # 无显著回撤：满分 × min(1, 成立年限/3)
}

# ---------------------------------------------------------------- 固收+ 经理资质调节 R3
# 阈值语义 = "被超过比例"（0=全胜）：每年被超过 ≤50%（前半）且平均 ≤33.3%（前1/3）。
# 2026-10-19 修复：原实现直接拿收益分位（越高越好）与阈值比大小，方向反了——339 只
# 常年垫底的基金拿精英容差、95 只真精英（含 110017/160323）反而没有；现改为 1-r 后比较。
QUALIFIER = {"yearly_rank_le": 0.50, "avg_rank_le": 0.333, "min_years": 3,
             "tol_full_warn": 0.13, "tol_full_max": 0.15,
             "tol_plain_warn": 0.08, "tol_plain_max": 0.10}

# ---------------------------------------------------------------- 回撤分档打分（固收+）
FIXED_PLUS_MDD = {"full_mark_at": 0.04, "step_linear": True}

# ---------------------------------------------------------------- 红线 R7（封顶 60 分）
REDLINES = {
    "mgr_tenure_days": 180,                    # R7-1 任职 <6个月
    "scale_floor_yi": 0.5,                     # R7-2 规模 <5000万
    "mdd_abs_threshold": {                     # R7-3 双条件
        "short_bond": 0.003, "long_bond": 0.015},  # 固收+ 用调节后上限
    "single_day_abs_floor": {                  # R7-4 单日跌幅绝对下限
        "short_bond": 0.0010, "long_bond": 0.0030, "fixed_plus": 0.015},
    "single_day_mult": 3.0, "single_day_recover_days": 20,
    "unrecovered_days": 240,                   # R7-5 固收+ 深回撤未修复
    "score_cap": 60,
}

# ---------------------------------------------------------------- 修正 R8 / 星级 R9
ADJUST = {"long跑_years": 5, "long跑_bonus": 2, "adjust_cap": 5}

STARS = {
    # (分数下限, 同类占比上限)
    5: (85, 0.10), 4: (75, 0.25), 3: (60, None), 2: (45, None), 1: (0, None),
}
STARS_MIN_POOL = 20   # 池样本 <20 时豁免占比配额（floor 后配额归零会误伤高分基金）

# ---------------------------------------------------------------- 稳定性 §6
STABILITY = {"smooth_window": 6,       # L3 滑动中位（期）
             "downgrade_confirm": 2}   # L4 降档需连续 N 期

# ---------------------------------------------------------------- 数据不足 R2
HISTORY = {"min_pool_years": 1, "min_star_years": 3, "full_window_years": 5}

# ---------------------------------------------------------------- 短债两段式筛选（docs/07-短债筛选标准.md）
# 用户 2026-10-15 口径：先按硬条件+性能条件筛出候选，再按分数排序取 TOP_N。
# 按池键控：将来接入中长债/固收+/指数基金时在此加对应池的筛选块即可。
# 阶段一 硬条件（同基金 A/B/C/D… 份额合并计算，不分开单独计算）。
SCREENS = {
    "short_bond": {
        "pool": "short_bond",
        # S1~S5 硬条件
        "scale_min_yi": 5.0,          # S1 合并规模 ≥5 亿
        "mgr_career_min_years": 5.0,  # S2 现任基金经理从业 ≥5 年
        "mgr_tenure_min_days": 1095,  # S3 现任经理任本基金 ≥3 年
        "min_nav_years": 5.0,         # S4 成立 ≥5 年（保证近五年数据完整）
        "spike_max_gain": 0.01,       # S5 近5年任一单日涨幅 >1% 剔除（异常涨幅）
        "spike_window_years": 5,
        # P1~P2 性能条件（方案甲；字面规则"四项前40%×每年>55%"全阈值空集，放宽决策见 docs/07 §4）
        "comp_pct_min": 0.45,         # P1 四项分位均值在池内分位 ≥45%（即综合前 55%）
        "yearly_median_min": 0.55,    # P2 完整自然年收益分位的中位数 ≥55%
        "yearly_min_years": 4,        # P2 参与比较的完整年数下限
        "yearly_min_obs": 120,        # 完整年判定：年内净值点 ≥120 且存在上年基数
        "yearly_pool_min": 30,        # P2 单年分位比较的池内最少样本
        # 阶段三 排序
        "score_snapshot": "data/full_scores.json",  # 手动模式的分数快照（周更管线走筛后即时评分，不读此文件）
        "prefer_letter": "A",         # 排序取 A 类份额分数（用户口径；无 A 类则用代表份额）
    },
    # 中长期纯债（docs/09-中长债筛选与评分.md，2026-10-16 设计）：与短债同构（S1-S5+P1-P2），
    # 仅 S5 池别化——长债久期更长，>1% 单日涨幅占池 9.0%（短债仅 2.9%）且反向检验
    # 124 件 0 反向：真实行情日与单边坏点混合；>2% 为纯债经济不可能日涨幅（池内 p99=5.18%）。
    # 评分卡复用 POOL_WEIGHTS["long_bond"]（v5.1 IC 权重，docs/06 目标函数回测验证段与最优候选
    # u2 差 0.03pp 在噪声内），"稳为底+稍偏收益"由 IC 池收益族倾斜体现（ret_1y 9.97 vs 短债 7.69）。
    "long_bond": {
        "pool": "long_bond",
        # S1~S5 硬条件
        "scale_min_yi": 5.0,          # S1 合并规模 ≥5 亿（试算 945→697；≥10亿将排除 006980）
        "mgr_career_min_years": 5.0,  # S2 现任基金经理从业 ≥5 年（697→488）
        "mgr_tenure_min_days": 1095,  # S3 现任经理任本基金 ≥3 年（488→198，约束主力同短债）
        "min_nav_years": 5.0,         # S4 成立 ≥5 年（198→162）
        "spike_max_gain": 0.02,       # S5 近5年任一单日涨幅 >2% 剔除（长债专属；短债 1%）
        "spike_window_years": 5,
        # P1~P2 性能条件（与短债"方案甲"同阈值，长债池试算 P2 后约 74~80 只 ≥ TOP20）
        "comp_pct_min": 0.45,         # P1 四项分位均值在池内分位 ≥45%
        "yearly_median_min": 0.55,    # P2 完整自然年收益分位的中位数 ≥55%
        "yearly_min_years": 4,        # P2 参与比较的完整年数下限
        "yearly_min_obs": 120,        # 完整年判定：年内净值点 ≥120 且存在上年基数
        "yearly_pool_min": 30,        # P2 单年分位比较的池内最少样本
        # 阶段三 排序
        "score_snapshot": "data/full_scores.json",
        "prefer_letter": "A",         # 排序取 A 类份额分数（同短债口径）
    },
    # 固收+（docs/10-固收加筛选与评分.md，2026-10-18 设计定稿；2026-10-19 v1.1 修订）：
    # 池口径=偏债混合+混合二级+混合一级（面板 1493 只）；三池人群阶梯中属"超长期资金"档。
    # S6/S7 风险预算闸（v2 试验 2026-10-18，外部评审采纳）：S6 分级——普通经理 ≤10%、
    # R3 精英经理 ≤12%（筛选期用基金级逐年分位判定，与评分端容差同语义）；
    # S7 波动带 2.5%~8%（下限自 2% 上调：把"行为像纯债"的弱增强挤回纯债侧，纯度优先）；
    # S8 收益上限**删除**（"不追高收益"≠"限制收益"，卡玛/夏普已在评分端约束激进风格）。
    "fixed_plus": {
        "pool": "fixed_plus",
        # S1~S5 硬条件（与两债池同构，S5 阈值池别化）
        "scale_min_yi": 5.0,          # S1 合并规模 ≥5 亿
        "mgr_career_min_years": 5.0,  # S2 经理从业 ≥5 年
        "mgr_tenure_min_days": 1095,  # S3 现任经理任本基金 ≥3 年
        "min_nav_years": 5.0,         # S4 成立 ≥5 年（评分主因子需 5 年窗口）
        "spike_max_gain": 0.05,       # S5 近5年无单日涨幅 >5%
        "spike_window_years": 5,
        # S6~S7 固收+专属风险预算（v2）
        "mdd_5y_max": 0.10,           # S6 普通经理：近5年最大回撤 ≤10%（v2 自 8% 放宽：真权益暴露品种回撤普遍 8~12%）
        "mdd_5y_max_qualified": 0.12, # S6 精英经理（R3：逐年被超过≤50%且平均≤1/3）：≤12%
        "vol_5y_min": 0.025,          # S7 下限：近5年年化波动 ≥2.5%（v2 自 2% 上调，池纯度优先）
        "vol_5y_max": 0.08,           # S7 上限：近5年年化波动 ≤8%
        # P1~P2 性能条件（百分位基准=固收+合并全池）
        "comp_pct_min": 0.45,         # P1 四项分位均值的池内分位 ≥45%
        "yearly_median_min": 0.55,    # P2 完整自然年收益分位的中位数 ≥55%
        "yearly_min_years": 4,        # P2 参与比较的完整年数下限
        "yearly_min_obs": 120,        # 完整自然年判定
        "yearly_pool_min": 30,        # P2 单年分位比较的池内最少样本
        # 阶段三 排序
        "score_snapshot": "data/full_scores.json",
        "prefer_letter": "A",         # 排序取 A 类份额分数（同两债池口径）
    },
    # 将来扩展位："index": {...}
}
SCREEN = SCREENS["short_bond"]   # 便捷别名（现有代码/文档引用）

# ---------------------------------------------------------------- 周更输出（docs/08-周更与卡片.md）
TOP_N = 20          # 周更输出前 N 支（筛选结果超过 N 只取分数前 N）
SHARPE_RF = 0.015   # 夏普无风险利率（年化 1.5%，近似 1 年期利率；仅展示不计分，可调）

# 卡片/详情页展示配置
CARD = {
    "stage_windows": ["1m", "3m", "6m", "1y", "3y", "5y"],   # 详情页阶段收益窗口
    # 净值走势只下发 1y+5y（1y⊂5y 按日期完全重叠）；前端把 3y 从 5y 按日期切片，
    # 省 60% trend 体积（实测 data.js 1.07MB → ~300KB）。1y 保全量采样点保真。
    "trend_windows": ["1y", "5y"],
    "trend_max_points": 360,                                  # 走势每条曲线最大采样点（内嵌 JSON 体积控制）
    "categories": [   # 分类 tab（启用的分类由 PIPELINE.pools 供给数据）
        {"id": "short_bond", "name": "短债", "enabled": True},
        {"id": "long_bond", "name": "中长债", "enabled": True},
        {"id": "fixed_plus", "name": "固收+", "enabled": True},
        {"id": "index", "name": "指数", "enabled": False},
    ],
}

# 周更管线
PIPELINE = {
    # 每周刷新哪些池（其他池数据不动）；fixed_plus 2026-10-18 接入（docs/10 §六 启用步骤）
    "pools": ["short_bond", "long_bond", "fixed_plus"],
    "log_dir": "data",         # 执行日志目录
}

# ---------------------------------------------------------------- 数据清洗
# 天天基金复权净值偶混入单日异常跳变（+134%/+193%等坏点），一个坏点即可毁掉整只
# 基金的收益/回撤/波动。债券基金单日涨跌不可能超此阈值，超限视为坏点并中和（置0）。
NAV_GLITCH_RET = 0.20      # 单日 |收益率| 超过 20% 即判定为数据坏点
GLITCH_MIN_POINTS = 3      # 净值点数少于此值不做坏点清洗（避免误伤）
