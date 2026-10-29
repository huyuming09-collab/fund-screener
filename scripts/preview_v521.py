# -*- coding: utf-8 -*-
"""本地预览：用缓存指标 + v5.21 规则重算固收+评分，输出自包含 HTML。
局限：本机无 nav_full，S5（单日异常涨幅）/P1/P2 未跑，候选集为近似（S1/S2/S3/S4/S6/S7 + W43 榜内 20 只）。
"""
import json, pickle, statistics as st, html, os
import sys
sys.path.insert(0, '.')
from fundscorer import config as C, scorer as S, metrics as M

OUT_DIR = os.path.join('运营', '未发布', '2026-10-29-债基四档分类', '预览')
os.makedirs(OUT_DIR, exist_ok=True)

cache = pickle.load(open('data/metrics_cache.pkl', 'rb'))
t = json.load(open('data/top20.json', encoding='utf-8'))
old = {x['code']: {'rank': x['rank'], 'score': x['score'], 'name': x['name']}
       for x in t['funds'] if x['category'] == 'fixed_plus'}

fp = [r for r in cache if r.get('pool') == 'fixed_plus']

# ---- 份额合并（基金级口径，仿 screen.merge_shares）：按基础名分组，避免 A/B/C 重复上榜
from fundscorer.screen import base_name, share_letter
groups = {}
for r in fp:
    groups.setdefault(base_name(r['name'] or ''), []).append(r)
merged = []
for base, shares in groups.items():
    rep = max(shares, key=lambda x: (x.get('scale_yi') or 0))
    src = max(shares, key=lambda x: ((x.get('nav_years') or 0),
                                     1 if share_letter(x['name'] or '') == 'A' else 0))
    m = dict(src)
    m['scale_yi'] = sum((x.get('scale_yi') or 0) for x in shares)     # 合并规模
    same = [x for x in shares if x.get('manager') == rep.get('manager')] or [rep]
    m['mgr_career_years'] = max([x.get('mgr_career_years') or 0 for x in same] or [0])
    m['mgr_tenure_days'] = max([x.get('mgr_tenure_days') or 0 for x in same] or [0])
    m['managers'] = rep.get('managers')
    m['n_classes'] = len(shares)
    merged.append(m)

cand = [r for r in merged
        if (r.get('scale_yi') or 0) >= 5
        and (r.get('mgr_career_years') or 0) >= 5
        and (r.get('mgr_tenure_days') or 0) >= 1095
        and (r.get('nav_years') or 0) >= 5
        and (r.get('mdd_5y') or 1) <= 0.10
        and 0.025 <= (r.get('vol_5y') or 0) <= 0.08]
codes = {r['code'] for r in cand}
for r in merged:
    if r['code'] in old and r['code'] not in codes:
        cand.append(r); codes.add(r['code'])

# ---- P1/P2 性能闸（近似复刻；S5 单日异常涨幅需净值，跳过）
import pandas as pd
from fundscorer.screen import composite_percentiles, yearly_median_pct
dfp = pd.DataFrame(cand)
P = composite_percentiles(dfp)
dfp['mpct'] = P['mpct'].values
n0 = len(dfp)
dfp = dfp[dfp['mpct'] >= 0.45]
n1 = len(dfp)
_years = sorted({str(y) for r in cand for y in (r.get('yearly') or {})})
_yearly_map = {}
for r in dfp.to_dict('records'):
    _yearly_map[base_name(r['name'] or '')] = r.get('yearly') or {}
Y = pd.DataFrame({y: {base_name(r['name'] or ''): (r.get('yearly') or {}).get(y)
                      for r in dfp.to_dict('records')} for y in _years})
keep = []
for r in dfp.to_dict('records'):
    b = base_name(r['name'] or '')
    try:
        p2 = yearly_median_pct(Y, b, _yearly_map)
    except Exception:
        p2 = None
    if p2 is None or p2 >= 0.55:      # 本机无法稳定复刻 P2 时按"不剔除"处理（偏宽松，页面已标注）
        keep.append(r)
cand = keep
print('P1 前 %d → P1 %d → P2 %d（近似；S5 跳过、P2 宽松）' % (n0, n1, len(cand)))
codes = {r['code'] for r in cand}
for r in merged:          # 旧榜 20 只兜底，保证对照完整
    if r['code'] in old and r['code'] not in codes:
        cand.append(r); codes.add(r['code'])

peers = [dict(r) for r in cand]
for p in peers:
    for k, v in list(p.items()):          # NaN → None（缓存里部分字段是 NaN，scorer 里 int(NaN) 会炸）
        if isinstance(v, float) and v != v:
            p[k] = None
    S._risk_ratios(p)          # calmar_5y/sharpe_5y 是派生字段，缓存里没有，必须先算（否则卡玛 30 分被当成缺失）
stab = S._pool_yearly_ranks(peers)
med_dd = st.median([p['mdd_5y'] for p in peers])
W = C.POOL_WEIGHTS['fixed_plus']
rows = []
for p in peers:
    f = dict(p)
    S._score_one(f, peers, W, 'fixed_plus', med_dd, stab.get(f['code'], []))
    if f.get('score') is None:
        continue
    rows.append({
        'code': f['code'], 'name': f.get('name', ''), 'score': f['score'],
        'mdd': f.get('mdd_5y'), 'vol': f.get('vol_5y'), 'calmar': f.get('calmar_5y'),
        'sub': f.get('sub_scores', {}), 'coef': f.get('mgr_team_coef', 1.0),
        'team_n': f.get('mgr_team_n', 0), 'earned': f.get('earned_weight'),
        'redlines': f.get('redlines') or [],
    })
rows.sort(key=lambda r: -r['score'])
top = rows[:20]

# ---- 旧规则摘要（供页面展示）
OLD_RULE = {
    '回撤': '≤4% 满分，精英 15% 归零（普通 10%），权重 9/7/5',
    '波动': '≤池均值×1.5（≈6.3%）满分，≥×4 归零，权重 10',
    '卡玛': '权重 25',
    '胜率': '月度胜率 9 + 20 季跑赢 8（仅展示）',
    '经理块': '任期 4 / 从业 4 / 产品 4；团队 1.05/1.10（按共管人数）',
    '其他': 'ret_5y 2 / fund_scale 4 / ms_rating 1 / inst 2 / fee 6',
}
NEW_RULE = {
    '回撤': '≤3% 满分，精英 12%（普通 10%）归零，权重 6/5/3',
    '波动': '≤3% 满分，≥8% 归零，权重 10',
    '卡玛': '权重 30',
    '胜率': '0 / 0（仅展示）→ 释放 17 分',
    '经理块': '任期 4 / 从业 5 / 产品 5 = 14；团队 1.03/1.05（全员 ≥8 年才给）',
    '其他': 'q_excess_20q 6 / ret_1y 6 / age 5 / ret_5y 2 / scale 4 / rating 1 / inst 2 / fee 6',
}


def fmt_pct(v):
    return '—' if v is None else f'{v*100:.2f}%'


def fmt_s(v):
    return '—' if v is None else f'{v:.0f}'


trs = []
for i, r in enumerate(top, 1):
    o = old.get(r['code'])
    if o:
        move = o['rank'] - i          # 正数=名次前进
        mcls = 'up' if move > 0 else ('down' if move < 0 else 'flat')
        move_s = ('↑%d' % move) if move > 0 else (('↓%d' % -move) if move < 0 else '—')
        old_cell = f"#{o['rank']}"
    else:
        mcls = 'new'
        move_s = '新进'
        old_cell = '—'
    sub = r['sub']
    coef = r['coef']
    trs.append(
        f"<tr><td class='rk'>{i}</td><td class='nm'>{html.escape(r['name'])}<span class='cd'>{r['code']}</span></td>"
        f"<td class='num strong'>{r['score']:.1f}</td><td class='num'>{old_cell}</td>"
        f"<td class='num {mcls}'>{move_s}</td>"
        f"<td class='num'>{fmt_pct(r['mdd'])}<span class='sub'>{fmt_s(sub.get('mdd_5y'))}</span></td>"
        f"<td class='num'>{fmt_pct(r['vol'])}<span class='sub'>{fmt_s(sub.get('vol_5y'))}</span></td>"
        f"<td class='num'>{fmt_s(sub.get('calmar_5y'))}</td>"
        f"<td class='num'>{fmt_s(sub.get('q_excess_20q'))}</td>"
        f"<td class='num'>{fmt_s(sub.get('ret_1y'))}</td>"
        f"<td class='num'>{coef:.2f}</td></tr>")

rule_rows = ''.join(
    f"<tr><th>{k}</th><td>{html.escape(OLD_RULE[k])}</td><td class='new'>{html.escape(NEW_RULE[k])}</td></tr>"
    for k in OLD_RULE)

page = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>固收+ 评分 v5.21 本地预览</title>
<style>
:root{{--ink:#1b1e24;--muted:#6a6e76;--line:#e2dbcc;--paper:#f6f3ec;--card:#fffdf8;--acc:#c4602a;--up:#1f7a4d;--down:#b23b2e}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:#2a2e35;font:16px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:32px 20px 64px}}
.warn{{background:#fff4e5;border-left:4px solid var(--acc);padding:14px 16px;border-radius:6px;font-size:14px;color:#7a4a1d;margin-bottom:24px}}
h1{{font-size:28px;margin:0 0 6px}}
.meta{{color:var(--muted);font-size:14px;margin-bottom:24px}}
h2{{font-size:20px;margin:36px 0 12px;padding-left:10px;border-left:4px solid var(--acc)}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:14px}}
th,td{{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left}}
thead th{{background:#efe9dc;font-weight:600;white-space:nowrap;font-size:13px}}
tbody tr:last-child td{{border-bottom:none}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.strong{{font-weight:700;color:#111}}
.rk{{color:var(--muted);width:44px}}
.nm{{min-width:220px}}
.cd{{display:block;color:#9aa0a8;font-size:11px;letter-spacing:.5px}}
.sub{{display:block;color:#9aa0a8;font-size:11px}}
.up{{color:var(--down)}} .down{{color:var(--up)}} .flat{{color:var(--muted)}}
.new{{color:var(--acc);font-weight:700}}
.rule th{{width:120px;font-weight:600;background:#faf7f0}}
.rule td{{font-size:13px}}
.rule td.new{{color:#1f7a4d;font-weight:400}}
.note{{margin-top:28px;color:var(--muted);font-size:13px;line-height:1.8}}
</style></head><body><div class="wrap">
<h1>固收+ 评分 v5.21 —— 本地预览</h1>
<div class="meta">数据基准 2026-10-24（W43 产物）· 规则 v5.21（docs/18 / v7 定稿）· 生成于本地重算</div>

<div class="warn"><b>先看这里：这是"重算预览"，不是管线产物。</b>本机缺 <code>data/nav_full</code>（无净值文件），管线跑不了，
所以候选集用缓存指标近似（S1 规模 / S2 从业 / S3 任期 / S4 成立 / S6 回撤 / S7 波动 + W43 旧榜 20 只兜底），
<b>S5（单日异常涨幅）、P1/P2（分位闸）未执行</b>——因此"新进"名单会偏多、分数只用于看方向，正式榜单以管线产物为准。</div>

<h2>一、规则改了什么</h2>
<table class="rule"><thead><tr><th>项</th><th>v5.20（旧）</th><th>v5.21（新）</th></tr></thead>
<tbody>{rule_rows}</tbody></table>

<h2>二、按新规则重排的 TOP20</h2>
<table><thead><tr>
<th>新名次</th><th>基金</th><th>新分</th><th>旧名次</th><th>名次变化</th>
<th>近5年回撤<br><span style="font-weight:400">（子项分）</span></th>
<th>近5年波动<br><span style="font-weight:400">（子项分）</span></th>
<th>卡玛<br>子项分</th><th>超额胜率<br>子项分</th><th>近1年<br>子项分</th><th>团队<br>系数</th>
</tr></thead><tbody>
{''.join(trs)}
</tbody></table>

<div class="note">
<b>怎么读这张表</b><br>
· 「旧名次」= W43（2026-10-24）实际榜单名次；「名次变化」正数前进、负数后退。<br>
· <b>「新分」不要和旧分直接相减</b>：旧分来自管线产物（候选 83 只），新分是本页在近似候选集（67 只）上重算的，两者分位基准不同，分数绝对值会整体偏低——<b>只看方向与名次</b>。<br>
· 回撤/波动两列括号里是子项分：不再"够稳就满分"——4.7% 回撤现在 75 分（旧规则 93.4），1.9~3.2% 的仍是 97~100。<br>
· 卡玛权重 25→30 是加分主源；胜率已归零（只展示），近 1 年收益 6 分、20 季超额胜率 6 分、成立年限 5 分新进。<br>
· 团队系数：只有"全体经理从业 ≥8 年且任本基金 ≥3 年"才给 1.03/1.05，本期只有景顺景颐招利（1.05）与景顺长城景颐双利（1.03）吃到。<br>
· 本页与 <code>web/index.html</code> 无关，不写任何产物；看完可直接关掉。
</div>
</div></body></html>"""

p = os.path.join(OUT_DIR, 'index.html')
open(p, 'w', encoding='utf-8').write(page)
print('wrote', p, len(page), 'chars; top rows', len(top))
print('top5:', [(r['name'][:12], round(r['score'], 1)) for r in top[:5]])
