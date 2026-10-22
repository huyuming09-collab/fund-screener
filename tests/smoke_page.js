// DOM 桩冒烟：响应式 + 免责底栏 + 费率/环比/分类渲染 + 多池 tab 切换 + 3y 切片 + DPR + 无 NaN
// 用法（仓库根目录）：node tests/smoke_page.js
// 依赖 web/index.html + web/data.js（单池旧 data.js 与多池新 data.js 都要全绿）。
const fs = require('fs');
const html = fs.readFileSync('web/index.html','utf8');
const dataJs = fs.readFileSync('web/data.js','utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

const classes = new Set();
const mkEl = () => ({ innerHTML:'', textContent:'', style:{}, dataset:{}, onclick:null, disabled:false, scrollTop:0, hidden:false, addEventListener(){},
  classList:{ add:c=>classes.add(c), remove:c=>classes.delete(c), contains:c=>classes.has(c), toggle:()=>{} } });
const els = {};
global.document = {
  getElementById: id => els[id] ||= mkEl(),
  querySelectorAll: () => [],
  body: { style:{} },
  addEventListener(){},
};
global.window = { devicePixelRatio: 2 };
const ctxStub = new Proxy({}, { get: (t,k) => {
  if (k==='createLinearGradient') return ()=>({addColorStop(){}});
  if (k==='measureText') return ()=>({width:20});
  if (typeof k==='string') return ()=>{};
}});
els['trend'] = { getContext: ()=>ctxStub, width:0, height:0, clientWidth:448, clientHeight:150 };
global.requestAnimationFrame = fn => fn();
global.setTimeout = (fn) => fn();

let fail = 0;
const chk = (name, ok) => { console.log((ok?'✓':'✗ FAIL')+' '+name); if(!ok) fail++; };

eval(dataJs + '\n' + script + `
;renderTabs(ACTIVE); renderList();

const shortFunds = (D.funds||[]).filter(f=>(f.category || D.category || 'short_bond')==='short_bond');
const longFunds  = (D.funds||[]).filter(f=>f.category==='long_bond');
const fpFunds    = (D.funds||[]).filter(f=>f.category==='fixed_plus');
const expectedN  = (D.pools && Object.keys(D.pools).length)
  ? Object.values(D.pools).reduce((s,p)=>s+p.n, 0) : D.n;

// 1. 渲染与详情：全部卡可开、无显示层泄漏、详情可开关
const bad = [];
for (const f of D.funds) {
  try { openDetail(f.code); } catch(e){ bad.push(f.code+': '+e.message); continue; }
  const txt = document.getElementById('d-body').innerHTML + document.getElementById('meta').innerHTML
            + document.getElementById('d-rank').textContent;
  if (/NaN|undefined|null年|null%/.test(txt)) bad.push(f.code+' 显示层泄漏');
}
chk('全量卡渲染无 NaN/undefined 泄漏', D.funds.length===expectedN && bad.length===0);
chk('卡数与契约一致（单池 D.n / 多池 pools 合计）', D.funds.length===expectedN);

// 2. 列表页常显免责底栏
const pd = document.getElementById('p-disc').textContent;
chk('列表页免责底栏有文案且来自 data.js', pd.length>30 && pd===D.disclaimer);
closeDetail();
chk('详情关闭正常', !document.getElementById('detail').classList.contains('show'));

// 3. 响应式/口径 CSS 与架构断言
const cssOK = /#phone\\{[^}]*width:100%[^}]*max-width:480px/.test(${JSON.stringify(html)})
  && /@media \\(min-width:640px\\)/.test(${JSON.stringify(html)})
  && /100dvh/.test(${JSON.stringify(html)});
chk('内容列：满宽、max-width 480px、640px 断点投影、dvh 高度', cssOK);
chk('滚动架构保留（main/detail 各自内部滚动）',
  /main\\{[^}]*overflow-y:auto/.test(${JSON.stringify(html)}) && /#detail\\{[^}]*overflow-y:auto/.test(${JSON.stringify(html)}));
chk('viewport 已放开缩放（无 user-scalable=no）',
  !/user-scalable=no/.test(${JSON.stringify(html)}) && /width=device-width, initial-scale=1.0/.test(${JSON.stringify(html)}));
chk('详情页免责声明区块保留', /id="d-disc"/.test(${JSON.stringify(html)}));

// 4. 卡片口径：无红线已删、合并规模、费率行
const listHTML = document.getElementById('list').innerHTML;
chk('无红线文案已删除', !listHTML.includes('无红线') && !html.includes('>无红线<'));
chk('列表卡显示合并规模', listHTML.includes('合并规模 '));
chk('列表卡显示费率行（买入+管理/年）', /费率 买入[\\d.]+% · 管理[\\d.]+%\\/年/.test(listHTML));
openDetail(shortFunds[0].code);
const db = document.getElementById('d-body').innerHTML;
chk('详情四行费率（买入/管理/托管/销售服务）',
  db.includes('买入费率') && db.includes('管理费率') && db.includes('托管费率') && db.includes('销售服务费率'));
chk('详情基金合并规模', db.includes('基金合并规模'));
chk('详情无申购+托管费旧口径', !db.includes('申购+托管费'));
chk('v5.16 共管系数展示封顶（贡献折系数前原值 + 共管×标注）', html.includes('共管×') && html.includes('coefOf'));
const den0 = (D.pools && D.pools[shortFunds[0].category] && D.pools[shortFunds[0].category].n) || D.n;
chk('详情头部名次（分母=所属池 n）',
  document.getElementById('d-rank').textContent.includes('第 '+shortFunds[0].rank+' 名 / '+den0));
chk('头部分类标签非硬编码', (() => {
  /* 2026-10-20 用户要求：基金代码移到标题行内（d-code-inline），scoreline 改为
     池名·风格·申购状态 → 校验代码精确 + 池名动态 */
  const ci = document.getElementById('d-code-inline').textContent;
  const t = document.getElementById('d-code').textContent;
  return ci === shortFunds[0].code && t.includes(catLabel(shortFunds[0].category));
})());
chk('同类分位标题动态分类', db.includes('同类分位（'+catLabel(shortFunds[0].category)+'池）'));

// 4b. v5.5（docs/12 §8/§9/§11）：风格标签 / 经理占席 / 共管 / 任期卡玛——均仅展示不计分
const styledAll = (D.funds||[]).filter(f=>f.style!=null);
chk('风格标签：有值且为四象限之一',
    styledAll.length>0 && styledAll.every(f=>['进取型','稳健型','防守型','震荡型'].includes(f.style)));
chk('列表卡风格徽标渲染（当前池）', /class="rk style /.test(listHTML));
chk('详情头部带风格后缀（无标签时可缺席）',
    !shortFunds[0].style || document.getElementById('d-code').textContent.includes('· '+shortFunds[0].style));
chk('详情经理区：任期卡玛（展示·不计分）',
    db.includes('任期卡玛') && db.includes('展示·不计分'));
chk('经理占席契约：逐只有值；当前池有占席≥2 则徽标渲染',
    shortFunds.every(f=>f.manager && f.manager.seats!=null)
    && (!shortFunds.some(f=>f.manager.seats>=2) || listHTML.includes('占席×')));
chk('共管字段契约：一律为数组（旧缓存空数组，前端按单经理渲染）',
    (D.funds||[]).every(f=>Array.isArray(f.manager.co_managers)));

// 5. 环比/新增：按 D.has_prev 分支，逐池检查（DOM 只渲染当前 tab → 合同按池限定，
//    否则"新增落在固收+、断言查中短债 DOM"会误报）
const baseList = document.getElementById('list').innerHTML;
const origHasPrev = D.has_prev;
if (D.has_prev) {
  const poolIds = D.pools ? Object.keys(D.pools) : ['short_bond'];
  let okAll = true;
  for (const pid of poolIds) {
    const tabFunds = (D.funds||[]).filter(f=>(f.category || D.category || 'short_bond')===pid);
    if (!tabFunds.length) continue;
    switchTab(pid);
    const h = document.getElementById('list').innerHTML;
    const anyDelta = tabFunds.some(f=>f.rank_delta!=null);
    const anyNew = tabFunds.some(f=>f.is_new);
    okAll = okAll && (!anyDelta || /class="rk (up|dn|flat)">/.test(h))
                  && (!anyNew || h.includes('rk new'));
  }
  switchTab('short_bond');
  chk('有历史：环比徽标/新增标记随契约渲染（逐池）', okAll);
} else {
  chk('首期列表无环比徽标/新增标记',
      !baseList.includes('rk up') && !baseList.includes('rk new') && !baseList.includes('新增'));
  chk('has_prev=false 契约', D.funds.every(f=>f.rank_delta==null && !f.is_new));
}
D.has_prev = true;
D.funds[0].rank_delta = 2; D.funds[1].rank_delta = -1; D.funds[2].is_new = true; D.funds[2].rank_delta = null;
renderList();
const l2 = document.getElementById('list').innerHTML;
chk('↑上升红徽标渲染', /class="rk up">↑2<\\/span>/.test(l2));
chk('↓下降绿徽标渲染', /class="rk dn">↓1<\\/span>/.test(l2));
chk('新增橙标渲染', /class="rk new">新增<\\/span>/.test(l2));
openDetail(D.funds[0].code);
chk('详情头部较上期后缀', /（较上期 ↑2）/.test(document.getElementById('d-rank').textContent));
openDetail(D.funds[2].code);
chk('详情新增后缀', /（新增）/.test(document.getElementById('d-rank').textContent));
// 还原
D.funds[0].rank_delta = null; D.funds[1].rank_delta = null;
D.funds[2].is_new = false; D.has_prev = origHasPrev; renderList();

// 6. 走势 3y 切片（data.js 只发 1y+5y）
chk('data.js trend 仅 1y+5y', D.funds.every(f => JSON.stringify(Object.keys(f.trend).sort()) === '["1y","5y"]'));
const f0 = D.funds[0];
const s3 = trendSeries(f0, '3y');
chk('3y 从 5y 按日期切片且点数合理', s3.length >= 2 && s3.length <= f0.trend['5y'].length
  && s3[s3.length-1][0] === f0.trend['5y'][f0.trend['5y'].length-1][0]
  && s3[s3.length-1][0].slice(0,4) - s3[0][0].slice(0,4) <= 4);
chk('3y 起点约 3 年前', Math.abs(s3[0][0].slice(0,4) - (f0.trend['5y'][f0.trend['5y'].length-1][0].slice(0,4) - 3)) <= 1);
chk('1y/5y 直接命中', trendSeries(f0,'1y').length>0 && trendSeries(f0,'5y').length>0);
// 旧版 data.js 含 3y 键时直接命中（前向兼容）
const legacy = {code:'X', trend:{'1y':f0.trend['1y'], '3y':f0.trend['1y'], '5y':f0.trend['5y']}};
chk('旧 data.js（含 3y 键）直接命中', trendSeries(legacy,'3y') === legacy.trend['3y']);

// 7. DPR canvas：devicePixelRatio=2 → 内部分辨率放大
const cv = document.getElementById('trend');
openDetail(D.funds[0].code);
chk('canvas DPR 放大（448×150 ×2）', cv.width === 896 && cv.height === 300);

// 8. meta 不再展示"筛出 N 只取前 M 名"（2026-10-17 改版：网页与导出图均删除该条）；data.js 契约字段保留
chk('meta 无"筛出…取前…名"', !document.getElementById('meta').innerHTML.includes('筛出'));
chk('meta 数据基准加粗标注（排名页数据时间，v5.14）', document.getElementById('meta').innerHTML.includes('class="asof"'));
chk('data.js 契约 n_screened/top_n 字段保留', D.n_screened != null && D.top_n != null);

// 9. 分类 tab：处理器绑定 + 多池切换过滤（单池旧 data.js 只断言处理器与回退）
const tabsHTML = document.getElementById('tabs').innerHTML;
const nEnabled  = D.categories.filter(c=>c.enabled).length;
const nDisabled = D.categories.filter(c=>!c.enabled).length;
// hidden 分类不渲染 tab（当前 0 个 hidden；断言保留以守未来隐藏开关）
  // hidden 分类不渲染 tab → 只数可见分类
  const visCats = D.categories.filter(c=>!(c&&c.hidden));
  chk('可见分类渲染 tab、hidden 分类不渲染',
      visCats.every(c => tabsHTML.includes(c.name))
      && D.categories.filter(c=>c&&c.hidden).every(c => !tabsHTML.includes(c.name)));
chk('禁用分类不可点击（toast 提示）', (tabsHTML.match(/toast\\('/g)||[]).length === nDisabled);
if (D.pools && D.pools.long_bond && longFunds.length) {
  // 池显示名从 data.js 派生（2026-10-19 改名 中短债/长债/固收+），不再写死
  const longLabel = (D.categories.find(c=>c.id==='long_bond')||{}).name || '长债';
  console.log('· 多池分支：' + (D.categories.find(c=>c.id==='short_bond')||{}).name + ' ' + shortFunds.length + ' 卡 / ' + longLabel + ' ' + longFunds.length + ' 卡');
  switchTab('long_bond');
  const h = document.getElementById('list').innerHTML;
  chk('长债 tab 只显该池卡片',
      longFunds.every(f=>h.includes(f.code)) && shortFunds.every(f=>!h.includes(f.code)));
  const seg = document.getElementById('tabs').innerHTML.split('<button').find(s=>s.includes(longLabel)) || '';
  chk('长债 tab 激活态', /class="tab active/.test(seg));
  const m = document.getElementById('meta').innerHTML;
  chk('meta 显示长债池口径（无筛出条）',
      m.includes(longLabel+'池 本期上榜 <b>'+longFunds.length+'</b> 只')
      && !m.includes('筛出'));
  openDetail(longFunds[0].code);
  chk('长债详情分母 = 池内 n',
      document.getElementById('d-rank').textContent.includes('第 '+longFunds[0].rank+' 名 / '+D.pools.long_bond.n));
  const db2 = document.getElementById('d-body').innerHTML;
  chk('长债详情 S5 阈值按池显示 2%', db2.includes('筛选阈值 &lt;2%'));
  chk('长债详情入选依据阈值（45%/55%/5亿）',
      db2.includes('（≥45%）') && db2.includes('（≥55%）') && db2.includes('（≥5亿）'));
  chk('长债详情同类分位标题', db2.includes('同类分位（'+longLabel+'池）'));
  switchTab('short_bond');
  const h2 = document.getElementById('list').innerHTML;
  chk('切回短债 tab 恢复短债卡片',
      shortFunds.every(f=>h2.includes(f.code)) && longFunds.every(f=>!h2.includes(f.code)));
  openDetail(shortFunds[0].code);
  chk('短债详情 S5 阈值仍 1%',
      document.getElementById('d-body').innerHTML.includes('筛选阈值 &lt;1%'));
  chk('短债详情无风险预算行（三闸仅固收+）',
      !document.getElementById('d-body').innerHTML.includes('风险预算'));
  // 指数基金穿插（2026-10-19）：标记、角标、详情口径与插值锚点
  const idxAll = (D.funds||[]).filter(f=>f.is_index);
  const actAll = (D.funds||[]).filter(f=>!f.is_index);
  // 债指总开关关闭时（INDEX.enabled=false）入榜指数为 0 属合法状态 → 断言不强求 >0
  chk('is_index：固收+无误标、主动数不为0、主动无插值锚点',
      actAll.length>0
      && (D.funds||[]).filter(f=>f.category==='fixed_plus').every(f=>!f.is_index)
      && actAll.every(f=>f.score_basis==null));
  if (idxAll.length) {
    const ix = idxAll[0];
    switchTab(ix.category);
    const segIx = document.getElementById('list').innerHTML
      .split('<div class="fundcard"').find(s=>s.includes(ix.code)) || '';
    chk('指数卡片显示"指数"徽标', segIx.includes('rk idx') && segIx.includes('>指数<'));
    // v5.12 单列债指（docs/17 §9）：卡脚 = 单列榜口径（旧"穿插评估"文案已随结构拍板废弃）
    chk('指数卡片不显示经理从业、改显示单列评估口径',
        segIx.includes('债指单列榜') && segIx.includes('经理块基线')
        && !segIx.includes('经理从业'));
    const ac = actAll.find(f=>f.category===ix.category);
    if (ac) {
      const segAc = document.getElementById('list').innerHTML
        .split('<div class="fundcard"').find(s=>s.includes(ac.code)) || '';
      chk('主动卡片仍显示经理从业、无指数徽标',
          segAc.includes('经理从业') && !segAc.includes('rk idx'));
    }
    openDetail(ix.code);
    const dbI = document.getElementById('d-body').innerHTML;
    chk('指数详情用"指数评估口径"替代"基金经理"（含指数族行）',
        dbI.includes('<h3>指数评估口径') && !dbI.includes('<h3>基金经理')
        && dbI.includes('指数族 / 久期段') && dbI.includes('公司非货规模'));
    // 断言用 <h3> 锚定：裸串会匹配到 HTML 注释（<!-- 评分明细… -->）
    // v5.12：评分明细 = 指数评估卡分项贡献（merged 回退路径的插值锚点渲染休眠）
    chk('指数详情"评分明细"展示评估卡分项贡献而非插值锚点',
        dbI.includes('<h3>评分明细') && !dbI.includes('<h3>评分依据')
        && dbI.includes('指数评估卡 · 同尺贡献分+经理块基线') && dbI.includes('总费率')
        && !dbI.includes('下邻锚点') && !dbI.includes('线性插值得分'));
    if (ac) {
      openDetail(ac.code);
      const dbA = document.getElementById('d-body').innerHTML;
      chk('主动详情仍显示"基金经理"+"评分明细·分项贡献分"',
          dbA.includes('<h3>基金经理') && dbA.includes('<h3>评分明细')
          && dbA.includes('分项贡献分') && !dbA.includes('<h3>评分依据'));
    }
    switchTab('short_bond');
  } else {
    console.log('· 无指数穿插标记（旧 data.js 或本期指数基金全部落榜）');
  }
  if (fpFunds.length) {          // 固收+第三池接入（docs/10，2026-10-18）
    console.log('· 三池分支：固收+ ' + fpFunds.length + ' 卡');
    switchTab('fixed_plus');
    const h3 = document.getElementById('list').innerHTML;
    chk('固收+ tab 只显该池卡片',
        fpFunds.every(f=>h3.includes(f.code)) && shortFunds.every(f=>!h3.includes(f.code)));
    const m3 = document.getElementById('meta').innerHTML;
    chk('meta 显示固收+池口径', m3.includes('固收+池 本期上榜 <b>'+fpFunds.length+'</b> 只'));
    openDetail(fpFunds[0].code);
    const db3 = document.getElementById('d-body').innerHTML;
    chk('固收+详情 S5 阈值按池显示 5%', db3.includes('筛选阈值 &lt;5%'));
    chk('固收+详情风险预算行（v2：S6 10%精英12%、S7 带2.5%~8%、无 S8）',
        db3.includes('风险预算') && db3.includes('≤10%') && db3.includes('2.5%~8%')
        && !db3.includes('&lt;8%'));
    switchTab('short_bond');
    const h4 = document.getElementById('list').innerHTML;
    chk('切回短债后固收+卡片消失',
        shortFunds.every(f=>h4.includes(f.code)) && fpFunds.every(f=>!h4.includes(f.code)));
  }
  // v5.12 单列债指（docs/17 §9）：分组头 / 组内名次分母 / 同指数其他优秀基金；v5.17 起开放对比按钮
  switchTab('index');
  const listIx = document.getElementById('list').innerHTML;
  // v5.14（2026-10-21）：债指分位与本池主动候选同尺（"业绩差不多分数也差不多"）+ 去截断全显示
  chk('债指 tab 分组头（评分与本池主动榜同尺，跨池不可比）',
      listIx.includes('债指 · ') && listIx.includes('评分与本池主动榜同尺'));
  const ixWithPeers = (D.funds||[]).find(f=>f.is_index && (f.index_peers||[]).length);
  if (ixWithPeers) {
    openDetail(ixWithPeers.code);
    const dbP = document.getElementById('d-body').innerHTML;
    chk('详情"同指数其他优秀基金"块渲染且注明口径',
        dbP.includes('<h3>同指数其他优秀基金')
        && dbP.includes('同一跟踪指数只在榜单展示 1 只')
        && dbP.includes('与主动榜分数不可比'));
    chk('债指详情名次分母 = 组内只数',
        document.getElementById('d-rank').textContent
          .includes('第 '+ixWithPeers.rank+' 名 / '+ixWithPeers.idx_group_n));
    // 2026-10-22 用户拍板：债指开放"加入对比"（同池=债指池内可比；v5.17 场外化同轮）
    chk('指数卡显示"加入对比"按钮',
        (listIx.split('<div class="fundcard"').find(s=>s.includes(ixWithPeers.code)) || '')
          .includes('class="pick"'));
  }
  switchTab('short_bond');
  chk('未知分类切换被忽略', (switchTab('nonexistent_cat'), ACTIVE==='short_bond'));
} else {
  console.log('· 单池 data.js：多池切换分支未适用（跑完周更管线后应出现多池分支）');
  switchTab('short_bond');
  chk('单池下 tab 切换不丢卡',
      document.getElementById('list').innerHTML.includes(shortFunds[0].code));
}

// 10. 桌面适配（≥1024px）+ 隐藏导出模式（?export=，页面无入口）
chk('桌面断点 ≥1024px：宽容器 + 榜单双列 + 详情居中面板',
  /@media \\(min-width:1024px\\)/.test(${JSON.stringify(html)})
  && /#phone\\{max-width:1180px/.test(${JSON.stringify(html)})
  && /#list\\{display:grid;grid-template-columns:1fr 1fr/.test(${JSON.stringify(html)})
  && /#detail\\{left:50%;right:auto;width:min\\(880px,92vw\\)/.test(${JSON.stringify(html)}));
const bodyOnly = ${JSON.stringify(html)}.replace(/<script>[\\s\\S]*?<\\/script>/g,'')
  .replace(/<style>[\\s\\S]*?<\\/style>/g,'').replace(/<!--([\\s\\S]*?)-->/g,'');
chk('导出模式在页面上无可见入口（无 ?export 链接/按钮）',
  !/\\?export=/.test(bodyOnly) && !/(href|onclick)=[^>]*export/i.test(bodyOnly));
chk('EXPORT_SIZES 契约（xhs 1080×1620/20行，wide 1200×800/10行）',
  EXPORT_SIZES.xhs.w===1080 && EXPORT_SIZES.xhs.h===1620 && EXPORT_SIZES.xhs.rows===20
  && EXPORT_SIZES.wide.w===1200 && EXPORT_SIZES.wide.h===800 && EXPORT_SIZES.wide.rows===10);
const xsAll = exportRows('short_bond'), lgAll = exportRows('long_bond');
chk('exportRows 按池过滤并按名次升序',
  xsAll.length>0 && xsAll.every(f=>(f.category||'short_bond')==='short_bond')
  && xsAll.every((f,i)=>i===0 || xsAll[i-1].rank<=f.rank)
  && exportRows('short_bond',5).length===5);
const cv2 = {width:0, height:0, getContext:()=>ctxStub};
const infoW = drawExport(cv2,'long_bond','wide');
chk('drawExport wide：画布 1200×800、行数=min(10, 池内数)',
  cv2.width===1200 && cv2.height===800 && infoW.rows===Math.min(10, lgAll.length) && infoW.rowH>30);
const infoP = drawExport(cv2,'short_bond','xhs');
chk('drawExport xhs：画布 1080×1620、行数=min(20, 池内数)、行高足够两行文字',
  cv2.width===1080 && cv2.height===1620 && infoP.rows===Math.min(20, xsAll.length) && infoP.rowH>=46);
chk('_deltaTxt 环比分支（↑/↓/新增/—/无历史空串）',
  _deltaTxt({rank_delta:2},true)==='↑2' && _deltaTxt({rank_delta:-1},true)==='↓1'
  && _deltaTxt({is_new:true,rank_delta:null},true)==='新增'
  && _deltaTxt({rank_delta:null},true)==='—' && _deltaTxt({rank_delta:3},false)==='');
const poolsExp = exportPools();
chk('exportPools：启用且有数据的池（多池=中短债+长债+固收+，旧单池回退默认池）',
  poolsExp.length >= (D.pools ? 2 : 1) && poolsExp.every(p => p && p.id && p.name)
  && (!D.pools || (poolsExp.some(p=>p.id==='short_bond') && poolsExp.some(p=>p.id==='long_bond'))));
chk('drawExport 遍历全部可导出池：每池都有行且画布为请求尺寸',
  poolsExp.every(p => { const info = drawExport(cv2, p.id, 'xhs');
    return info.rows > 0 && cv2.width === 1080 && cv2.height === 1620; }));
let drawOK = true;
try { drawExport(cv2,'long_bond','xhs'); drawExport(cv2,'not_a_pool','wide'); } catch(e){ drawOK = false; }
chk('drawExport 容错：未知池/空池不抛异常', drawOK);
chk('initExport 无 URL 参数时不进入导出模式', initExport() === false);

// 11. 池内横向对比（榜内勾选 ≤5 只、仅同池、校验行可复算）
if (shortFunds.length >= 2) {
  CMP = []; renderPick(); closeDetail();
  pickTgl(shortFunds[0].code); pickTgl(shortFunds[1].code);
  chk('勾选两只后浮条出现、对比按钮可用', CMP.length===2 && !document.getElementById('cmp-open').disabled);
  chk('勾选不触发详情面板', !document.getElementById('detail').classList.contains('show'));
  for (let i=2;i<6;i++) if (shortFunds[i]) pickTgl(shortFunds[i].code);
  chk('上限 5 只：选满后再点被拒', CMP.length===5);
  openCmp();
  const cb = document.getElementById('cmp-body').innerHTML;
  chk('对比表无 NaN/undefined 泄漏', !/NaN|undefined/.test(cb));
  chk('对比表含四分区与校验行',
      cb.includes('综合概览') && cb.includes('评分明细') && cb.includes('关键指标')
      && cb.includes('Σ 贡献分') && cb.includes('= 卡片上的评分'));
  const selCmp = CMP.map(c=>D.funds.find(f=>f.code===c));
  const tsvCmp = cmpTSV(selCmp, null, 0);
  const anySeat = selCmp.some(f=>f.manager && f.manager.seats>=2);
  chk('对比表含风格/经理行（v5.5 仅展示）',
      cb.includes('>风格<') && cb.includes('>经理<')
      && tsvCmp.includes('风格') && tsvCmp.includes('经理')
      && (/占席×\\d/.test(tsvCmp) === anySeat));
  chk('对比列数 = 选中只数', (cb.match(/<th><span class="nm">/g)||[]).length===CMP.length);
  const sel = CMP.map(c=>D.funds.find(f=>f.code===c));
  chk('契约字段齐备：raw_score/earned_weight/weights',
      sel.every(f=>f.raw_score!=null && f.earned_weight!=null)
      && Object.keys((D.pools||{})[sel[0].category||'short_bond'].weights||{}).length>0);
  if (sel.every(f=>f.raw_score!=null && f.earned_weight)) {
    chk('校验行复算 = 卡片评分',
        sel.every(f=>Math.abs(f.raw_score*100/f.earned_weight - f.score) < 0.02));
  }
  chk('复制文本表非空', cmpTSV(sel, null, 0).length > 40);
  closeCmp();
  switchTab('long_bond');
  chk('切池清空对比选择（跨池不可比）', CMP.length===0 && $('cmp-bar').hidden);
  switchTab('short_bond');
} else {
  chk('榜内样本 <2 只：对比断言跳过（合法）', true);
}

console.log(fail ? 'SMOKE FAIL: '+fail : 'SMOKE PASS');
process.exit(fail);
`);
