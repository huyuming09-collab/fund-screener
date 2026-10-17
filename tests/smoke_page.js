// DOM 桩冒烟：响应式 + 免责底栏 + 费率/环比/分类渲染 + 多池 tab 切换 + 3y 切片 + DPR + 无 NaN
// 用法（仓库根目录）：node tests/smoke_page.js
// 依赖 web/index.html + web/data.js（单池旧 data.js 与多池新 data.js 都要全绿）。
const fs = require('fs');
const html = fs.readFileSync('web/index.html','utf8');
const dataJs = fs.readFileSync('web/data.js','utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

const classes = new Set();
const mkEl = () => ({ innerHTML:'', textContent:'', style:{}, dataset:{}, onclick:null, disabled:false, scrollTop:0,
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
const den0 = (D.pools && D.pools[shortFunds[0].category] && D.pools[shortFunds[0].category].n) || D.n;
chk('详情头部名次（分母=所属池 n）',
  document.getElementById('d-rank').textContent.includes('第 '+shortFunds[0].rank+' 名 / '+den0));
chk('头部分类标签非硬编码',
  document.getElementById('d-code').textContent.endsWith('· '+catLabel(shortFunds[0].category)));
chk('同类分位标题动态分类', db.includes('同类分位（'+catLabel(shortFunds[0].category)+'池）'));

// 5. 环比/新增：按 D.has_prev 分支；再用合成数据覆盖 ↑/↓/新增 三个渲染分支
const baseList = document.getElementById('list').innerHTML;
const origHasPrev = D.has_prev;
if (D.has_prev) {
  const anyDelta = D.funds.some(f=>f.rank_delta!=null);
  const anyNew = D.funds.some(f=>f.is_new);
  chk('有历史：环比徽标/新增标记随契约渲染',
      (!anyDelta || /class="rk (up|dn|flat)">/.test(baseList))
      && (!anyNew || baseList.includes('rk new')));
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

// 8. meta 头部 n_screened（与 data.js 契约字段一致，不硬编码具体数值）
chk('meta 显式 n_screened', document.getElementById('meta').innerHTML.includes('筛出 ' + D.n_screened + ' 只取前 ' + D.top_n + ' 名'));

// 9. 分类 tab：处理器绑定 + 多池切换过滤（单池旧 data.js 只断言处理器与回退）
const tabsHTML = document.getElementById('tabs').innerHTML;
const nEnabled  = D.categories.filter(c=>c.enabled).length;
const nDisabled = D.categories.filter(c=>!c.enabled).length;
chk('启用分类均绑定 switchTab', (tabsHTML.match(/switchTab\\('/g)||[]).length === nEnabled);
chk('禁用分类不可点击（toast 占位）', (tabsHTML.match(/toast\\(\\)/g)||[]).length === nDisabled);
if (D.pools && D.pools.long_bond && longFunds.length) {
  console.log('· 多池分支：短债 ' + shortFunds.length + ' 卡 / 中长债 ' + longFunds.length + ' 卡');
  switchTab('long_bond');
  const h = document.getElementById('list').innerHTML;
  chk('中长债 tab 只显该池卡片',
      longFunds.every(f=>h.includes(f.code)) && shortFunds.every(f=>!h.includes(f.code)));
  const seg = document.getElementById('tabs').innerHTML.split('<button').find(s=>s.includes('中长债')) || '';
  chk('中长债 tab 激活态', /class="tab active/.test(seg));
  const m = document.getElementById('meta').innerHTML;
  chk('meta 显示中长债池口径',
      m.includes('中长债池 筛出 '+D.pools.long_bond.n_screened+' 只取前 '+D.pools.long_bond.top_n+' 名')
      && m.includes('本期上榜 <b>'+longFunds.length+'</b> 只'));
  openDetail(longFunds[0].code);
  chk('长债详情分母 = 池内 n',
      document.getElementById('d-rank').textContent.includes('第 '+longFunds[0].rank+' 名 / '+D.pools.long_bond.n));
  const db2 = document.getElementById('d-body').innerHTML;
  chk('长债详情 S5 阈值按池显示 2%', db2.includes('筛选阈值 &lt;2%'));
  chk('长债详情入选依据阈值（45%/55%/5亿）',
      db2.includes('（≥45%）') && db2.includes('（≥55%）') && db2.includes('（≥5亿）'));
  chk('长债详情同类分位标题', db2.includes('同类分位（中长债池）'));
  switchTab('short_bond');
  const h2 = document.getElementById('list').innerHTML;
  chk('切回短债 tab 恢复短债卡片',
      shortFunds.every(f=>h2.includes(f.code)) && longFunds.every(f=>!h2.includes(f.code)));
  openDetail(shortFunds[0].code);
  chk('短债详情 S5 阈值仍 1%',
      document.getElementById('d-body').innerHTML.includes('筛选阈值 &lt;1%'));
  chk('禁用分类切换被忽略', (switchTab('fixed_plus'), ACTIVE==='short_bond'));
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
chk('EXPORT_SIZES 契约（xhs 1080×1440/20行，wide 1200×800/10行）',
  EXPORT_SIZES.xhs.w===1080 && EXPORT_SIZES.xhs.h===1440 && EXPORT_SIZES.xhs.rows===20
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
chk('drawExport xhs：画布 1080×1440、行数=min(20, 池内数)、行高足够两行文字',
  cv2.width===1080 && cv2.height===1440 && infoP.rows===Math.min(20, xsAll.length) && infoP.rowH>=46);
chk('_deltaTxt 环比分支（↑/↓/新增/—/无历史空串）',
  _deltaTxt({rank_delta:2},true)==='↑2' && _deltaTxt({rank_delta:-1},true)==='↓1'
  && _deltaTxt({is_new:true,rank_delta:null},true)==='新增'
  && _deltaTxt({rank_delta:null},true)==='—' && _deltaTxt({rank_delta:3},false)==='');
const poolsExp = exportPools();
chk('exportPools：启用且有数据的池（多池=短债+中长债，旧单池回退默认池）',
  poolsExp.length >= (D.pools ? 2 : 1) && poolsExp.every(p => p && p.id && p.name)
  && (!D.pools || (poolsExp.some(p=>p.id==='short_bond') && poolsExp.some(p=>p.id==='long_bond'))));
chk('drawExport 遍历全部可导出池：每池都有行且画布为请求尺寸',
  poolsExp.every(p => { const info = drawExport(cv2, p.id, 'xhs');
    return info.rows > 0 && cv2.width === 1080 && cv2.height === 1440; }));
let drawOK = true;
try { drawExport(cv2,'long_bond','xhs'); drawExport(cv2,'not_a_pool','wide'); } catch(e){ drawOK = false; }
chk('drawExport 容错：未知池/空池不抛异常', drawOK);
chk('initExport 无 URL 参数时不进入导出模式', initExport() === false);

console.log(fail ? 'SMOKE FAIL: '+fail : 'SMOKE PASS');
process.exit(fail);
`);
