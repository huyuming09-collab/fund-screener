/* 市场情绪导出图模块（2026-11-04 用户拍板：与 index.html 导出工作台同址）
   入口（index.html）：
     ?export=market_score &size=xhs            周榜大图：环形情绪分+一句话解读（1080×1620）
     ?export=market_core  &size=xhs|wide       核心 4 详解：小红书竖版 / 公众号横版（wide 含常见范围）
     ?export=market_ref   &size=xhs|wide       参考 4 详解：同上
     ?export=market_ind   &key=erp&size=wide   单指标详解卡（公众号图文并排；key 可换 8 指标任一）
     ?export=market_cover &size=cover          公众号封面（1200×510，2.35:1）
   全部加 &bare=1 走无头截图出图；数据随 data.js 每周日更新，属每周固定模板。
   本文件自包含（LV/heatColor/concl 为导出侧副本），index.html 只负责解析与分派。 */
(function () {
  var M = window.CARD_DATA && window.CARD_DATA.market;
  var LV = {'寒冷':'#1a6fa8','偏冷':'#0b6b46','中性':'#4b5563','偏热':'#a85100','过热':'#b3261e'};
  function heatColor(h){
    if (h==null) return '#8a93a3';
    if (h<20) return '#1a6fa8'; if (h<40) return '#0b6b46';
    if (h<45) return '#4b5563'; if (h<70) return '#a85100'; return '#b3261e';
  }
  function concl(k,v){
    var p=v.pct; if(p==null) return '数据暂缺';
    if(k==='erp') return p>=70?'利差阔 → 股票性价比高':(p<=30?'利差窄 → 股票性价比低':'性价比中等');
    if(k==='pe') return p>=70?'估值偏贵':(p<=30?'估值偏低':'估值适中');
    if(k==='margin') return p>=70?'杠杆情绪高涨 → 偏热':(p<=30?'杠杆情绪低迷 → 偏冷':'杠杆情绪中等');
    if(k==='m1m2') return (v.value>0?'剪刀差为正 → 信用扩张':'剪刀差为负 → 信用收缩')+(p>=60?'（偏热）':p<=40?'（偏冷）':'');
    if(k==='ivix') return p>=70?'波动高 → 市场恐慌':(p<=30?'波动低 → 市场平静':'波动中等');
    if(k==='newfund') return p>=70?'发行火爆 → 情绪顶部':(p<=20?'发行冰点 → 情绪底部':'发行正常');
    if(k==='buffett') return p>=80?'证券化率高 → 长期偏贵':(p<=40?'证券化率低 → 长期偏低':'长期适中');
    if(k==='breaknet') return v.value>=10?'破净率高 → 底部区域':(v.value<=5?'破净率低 → 顶部区域':'破净率中等');
    return '';
  }
  /* 大图"一句话解读"用的短语（每指标 4 字内） */
  function shortChip(k,v){
    var p=v.pct;
    if(k==='erp') return p==null?'利差中性':(p>=70?'利差偏阔':(p<=30?'利差偏窄':'利差中性'));
    if(k==='margin') return p==null?'杠杆中性':(p>=70?'杠杆偏热':(p<=30?'杠杆偏冷':'杠杆中性'));
    if(k==='ivix') return p==null?'波动中等':(p<=30?'波动平静':(p>=70?'波动偏高':'波动中等'));
    if(k==='m1m2') return v.value==null?'资金面中性':(v.value>0?'信用扩张':'信用收缩');
    if(k==='pe') return p==null?'估值中性':(p>=70?'估值偏贵':(p<=30?'估值偏低':'估值适中'));
    if(k==='newfund') return p==null?'发行中性':(p>=70?'发行火爆':(p<=20?'发行冰点':'发行正常'));
    if(k==='buffett') return p==null?'证券化率中性':(p>=80?'长期偏贵':(p<=40?'长期偏低':'长期适中'));
    if(k==='breaknet') return v.value==null?'破净中性':(v.value>=10?'贴近大底':(v.value<=5?'高位区域':'破净中等'));
    return '';
  }

  var SIZES = { xhs:{w:1080,h:1620}, wide:{w:1200,h:800}, cover:{w:1200,h:510} };
  var NAMES = ['market_score','market_core','market_ref','market_ind','market_cover'];
  var BTN  = [['market_score','情绪大图'],['market_core','核心4详解'],['market_ref','参考4详解'],
              ['market_ind','单指标卡'],['market_cover','情绪封面']];

  function coreKeys(){
    return (M.core_keys && M.core_keys.length) ? M.core_keys.slice() : ['erp','margin','ivix','m1m2'];
  }
  function advKeys(){
    var c = coreKeys();
    return Object.keys(M.indicators).filter(function(k){ return c.indexOf(k)<0; });
  }
  function keysOf(name){ return name==='market_ref' ? advKeys() : coreKeys(); }

  /* ---------- 样式（注入 index.html，无需其维护） ---------- */
  var CSS = ''
  +'.mx{position:relative;overflow:hidden;background:#faf7f2;color:#26231e;'
  +  'font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif;}'
  +'.mx.s-xhs{width:1080px;height:1620px;padding:48px;box-sizing:border-box;}'
  +'.mx.s-wide{width:1200px;height:800px;padding:40px 48px;box-sizing:border-box;}'
  +'.mx.s-cover{width:1200px;height:510px;padding:44px 56px;box-sizing:border-box;}'
  +'.mx .hd{display:flex;justify-content:space-between;align-items:center;}'
  +'.mx .hd .tt{font-size:44px;font-weight:800;letter-spacing:1px;}'
  +'.mx .hd .cap{background:#ffe9d9;color:#b3541e;border-radius:999px;padding:9px 22px;font-size:25px;font-weight:700;white-space:nowrap;}'
  +'.mx .ft{position:absolute;left:48px;right:48px;bottom:40px;border-top:2px solid #efe7dc;'
  +  'padding-top:18px;display:flex;justify-content:space-between;align-items:flex-end;gap:18px;}'
  +'.mx .ft .l{font-size:22px;color:#8a8378;line-height:1.7;}'
  +'.mx .ft .r{font-size:25px;font-weight:800;color:#c05a28;white-space:nowrap;}'
  /* 大图 */
  +'.mx .sc-mid{display:flex;flex-direction:column;align-items:center;margin-top:74px;}'
  +'.mx .sc-ring{position:relative;width:430px;height:430px;}'
  +'.mx .sc-ring svg{width:100%;height:100%;transform:rotate(-90deg);}'
  +'.mx .sc-ring .cv{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;}'
  +'.mx .sc-ring .num{font-size:132px;font-weight:800;line-height:1;}'
  +'.mx .sc-ring .lv{font-size:40px;font-weight:700;margin-top:12px;}'
  +'.mx .sc-say{margin-top:56px;text-align:center;}'
  +'.mx .sc-say .chips{font-size:38px;font-weight:800;letter-spacing:2px;}'
  +'.mx .sc-say .sub{font-size:26px;color:#8a8378;margin-top:18px;line-height:1.75;}'
  +'.mx .sc-scale{width:820px;margin-top:52px;}'
  +'.mx .sc-scale .bar{display:flex;height:44px;border-radius:12px;overflow:hidden;}'
  +'.mx .sc-scale .bar i{display:block;height:100%;}'
  +'.mx .sc-scale .mk{display:flex;justify-content:space-between;font-size:22px;color:#8a8378;margin-top:8px;}'
  +'.mx .sc-scale .pos{position:relative;height:34px;}'
  +'.mx .sc-scale .tri{position:absolute;top:0;transform:translateX(-50%);font-size:28px;font-weight:800;color:#26231e;}'
  /* 指标详解（竖/横通用块） */
  +'.mx .lb{font-size:34px;font-weight:800;border-left:10px solid #ff6a00;padding-left:16px;margin:34px 0 6px;}'
  +'.mx .lb.ref{border-left-color:#8a93a3;}'
  +'.mx .it{padding:22px 0 20px;border-bottom:1px solid #efe7dc;}'
  +'.mx .it .r1{display:flex;justify-content:space-between;align-items:baseline;}'
  +'.mx .it .nm{font-size:32px;font-weight:700;color:#5c564c;}'
  +'.mx .it .vl{font-size:56px;font-weight:800;}'
  +'.mx .it .vl .u{font-size:24px;font-weight:600;color:#8a8378;margin-left:8px;}'
  +'.mx .it .r2{display:flex;align-items:center;gap:14px;margin-top:12px;}'
  +'.mx .it .trk{flex:1;height:16px;border-radius:8px;background:#f1ede6;overflow:hidden;}'
  +'.mx .it .trk i{display:block;height:100%;border-radius:8px;}'
  +'.mx .it .pc{font-size:26px;font-weight:800;min-width:86px;text-align:right;}'
  +'.mx .it .r3{display:flex;justify-content:space-between;gap:18px;margin-top:12px;align-items:baseline;}'
  +'.mx .it .cc{font-size:26px;font-weight:700;white-space:nowrap;}'
  +'.mx .it .ds{font-size:23px;color:#8a8378;line-height:1.6;text-align:right;}'
  +'.mx .it .rg{font-size:21px;color:#a9a294;margin-top:8px;}'
  /* 横版（公众号）：2×2 网格 + 去 lb（标题已带分组信息）+ 行距收紧 */
  +'.mx .g2{display:grid;grid-template-columns:1fr 1fr;column-gap:48px;}'
  +'.mx.s-wide .it{padding:15px 0 13px;}'
  +'.mx.s-wide .it .vl{font-size:44px;}'
  +'.mx.s-wide .it .r2{margin-top:8px;}'
  +'.mx.s-wide .it .r3{margin-top:8px;}'
  +'.mx.s-wide .it .cc{font-size:25px;}'
  +'.mx.s-wide .it .ds{font-size:21px;line-height:1.5;}'
  +'.mx.s-wide .it .rg{font-size:20px;margin-top:5px;}'
  +'.mx .blk{margin-bottom:16px;}'
  /* 单指标卡 */
  +'.mx .ind2{display:flex;gap:44px;margin-top:34px;}'
  +'.mx .ind2 .lc{width:44%;}'
  +'.mx .ind2 .rc{flex:1;}'
  +'.mx .ind2 .big{font-size:120px;font-weight:800;line-height:1;}'
  +'.mx .ind2 .big .u{font-size:34px;font-weight:600;color:#8a8378;margin-left:10px;}'
  +'.mx .ind2 .trk{height:22px;border-radius:11px;background:#f1ede6;overflow:hidden;margin-top:26px;}'
  +'.mx .ind2 .trk i{display:block;height:100%;border-radius:11px;}'
  +'.mx .ind2 .pc{font-size:30px;font-weight:800;margin-top:12px;text-align:right;}'
  +'.mx .ind2 .cc{font-size:30px;font-weight:700;margin-top:22px;}'
  +'.mx .blk .k{font-size:24px;font-weight:800;color:#c05a28;margin-bottom:6px;}'
  +'.mx .blk .v{font-size:23px;color:#5c564c;line-height:1.7;}'
  /* 封面 */
  +'.mx .cv-wrap{display:flex;height:100%;align-items:center;justify-content:space-between;gap:40px;}'
  +'.mx .cv-l .t1{font-size:74px;font-weight:800;letter-spacing:2px;}'
  +'.mx .cv-l .t2{font-size:27px;color:#8a8378;margin-top:18px;line-height:1.7;}'
  +'.mx .cv-l .t3{font-size:26px;font-weight:700;margin-top:20px;color:#5c564c;}'
  +'.mx .cv-ring{position:relative;width:360px;height:360px;flex:0 0 auto;}'
  +'.mx .cv-ring svg{width:100%;height:100%;transform:rotate(-90deg);}'
  +'.mx .cv-ring .cv{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;}'
  +'.mx .cv-ring .num{font-size:110px;font-weight:800;line-height:1;}'
  +'.mx .cv-ring .lv{font-size:34px;font-weight:700;margin-top:8px;}';

  function ensureStyle(){
    if (document.getElementById('mkt-exp-style')) return;
    var st = document.createElement('style');
    st.id = 'mkt-exp-style'; st.textContent = CSS;
    document.head.appendChild(st);
  }

  /* ---------- 小构件 ---------- */
  function ring(score, level, cls){
    var r = cls==='big' ? 176 : (cls==='cover' ? 132 : 120);
    var vb = cls==='big' ? 430 : (cls==='cover' ? 360 : 300);
    var sw = cls==='big' ? 34 : (cls==='cover' ? 26 : 26);
    var c = vb/2;
    var C = 2*Math.PI*r, fill = Math.max(0,Math.min(100,score))/100*C;
    var col = LV[level]||'#4b5563';
    var numSize = cls==='big' ? 132 : (cls==='cover' ? 110 : 92);
    var lvSize  = cls==='big' ? 40  : (cls==='cover' ? 34  : 30);
    return '<svg viewBox="0 0 '+vb+' '+vb+'">'
      +'<circle cx="'+c+'" cy="'+c+'" r="'+r+'" fill="none" stroke="#efe9e0" stroke-width="'+sw+'"/>'
      +'<circle cx="'+c+'" cy="'+c+'" r="'+r+'" fill="none" stroke="'+col+'" stroke-width="'+sw+'" stroke-linecap="round"'
      +' stroke-dasharray="'+fill.toFixed(1)+' '+C.toFixed(1)+'"/></svg>'
      +'<div class="cv"><span class="num" style="font-size:'+numSize+'px;color:'+col+'">'+score+'</span>'
      +'<span class="lv" style="font-size:'+lvSize+'px;color:'+col+'">'+level+'</span></div>';
  }
  function scaleBar(score){
    var segs = ['#1a6fa8','#0b6b46','#4b5563','#a85100','#b3261e'];
    var bar = segs.map(function(c){ return '<i style="width:20%;background:'+c+'"></i>'; }).join('');
    var mk = ['0','20','40','60','80','100'].map(function(m){ return '<span>'+m+'</span>'; }).join('');
    return '<div class="bar">'+bar+'</div><div class="mk">'+mk+'</div>'
      +'<div class="pos"><span class="tri" style="left:'+Math.max(2,Math.min(98,score))+'%">▼</span></div>';
  }
  function ind(k){
    var v = M.indicators[k]; if(!v) return '';
    var pct = v.pct==null?0:v.pct, hc = heatColor(v.heat);
    return {v:v, pct:pct, hc:hc};
  }
  function itemRow(k, withRange){
    var o = ind(k); if(!o) return '';
    var v = o.v;
    return '<div class="it">'
      +'<div class="r1"><span class="nm">'+v.label+'</span>'
      +'<span class="vl">'+(v.value==null?'—':v.value)+'<span class="u">'+(v.unit||'')+'</span></span></div>'
      +'<div class="r2"><span class="trk"><i style="width:'+o.pct+'%;background:'+o.hc+'"></i></span>'
      +'<span class="pc" style="color:'+o.hc+'">'+(v.pct==null?'—':v.pct.toFixed(0)+'%')+'</span></div>'
      +'<div class="r3"><span class="cc" style="color:'+o.hc+'">'+concl(k,v)+'</span>'
      +'<span class="ds">'+(v.desc||'')+'</span></div>'
      +(withRange && v.range ? '<div class="rg">常见范围：'+v.range+'</div>' : '')
      +'</div>';
  }
  function foot(l2){
    var asof = M.asof||'—';
    var dis = (window.CARD_DATA && window.CARD_DATA.disclaimer) || '历史业绩不代表未来，不构成投资建议';
    return '<div class="ft"><div class="l">情绪分与各指标分位按近 5 年历史自算 · 数据基准 '+asof+' · 每周日 19:00 更新<br>'
      +(l2||dis)+'</div><div class="r">@班马成长日记</div></div>';
  }
  function head(tt){
    return '<div class="hd"><div class="tt">'+tt+'</div><div class="cap">基准 '+(M.asof||'—')+'</div></div>';
  }

  /* ---------- 各图 ---------- */
  function buildScore(){
    var score=M.score, level=M.level;
    var chips = coreKeys().map(function(k){
      var v=M.indicators[k]; return v?shortChip(k,v):''; }).filter(Boolean).join(' · ');
    var w=M.weights||{};
    var wtxt=[['erp','股债利差'],['margin','两融'],['ivix','波动率'],['m1m2','资金面']]
      .map(function(p){ return p[1]+' '+Math.round((w[p[0]]||0)*100)+'%'; }).join(' · ');
    return '<div class="mx s-xhs">'+head('班马日记本 · 市场情绪周报')
      +'<div class="sc-mid">'
      +'<div class="sc-ring">'+ring(score, level, 'big')+'</div>'
      +'<div class="sc-say"><div class="chips">'+chips+'</div>'
      +'<div class="sub">本期情绪分 '+score+'，落在 '
      +(score<20?'20 以下寒冷':score<40?'20-40 偏冷':score<60?'40-60 中性':score<80?'60-80 偏热':'80 以上过热')
      +'区<br>合成口径：'+wtxt+'</div></div>'
      +'<div class="sc-scale">'+scaleBar(score)+'</div>'
      +'</div>'+foot()+'</div>';
  }
  function buildList(name, sizeKey){
    var ks = keysOf(name);
    var withRange = sizeKey==='wide';
    var isRef = name==='market_ref';
    var tt = isRef ? '市场情绪 · 参考 4 指标详解' : '市场情绪 · 核心 4 指标详解';
    /* 横版不画 lb 分组条（标题已带分组信息，省出的高度给 2×2 网格） */
    var lb = sizeKey==='wide' ? '' :
      '<div class="lb'+(isRef?' ref':'')+'">'+(isRef?'参考 4（独立观察，不进综合）':'核心 4（进综合，权重 40/25/20/15）')+'</div>';
    var items = ks.map(function(k){ return itemRow(k, withRange); }).join('');
    var l2 = isRef
      ? '四个参考指标不进综合，仅作独立观察；历史业绩不代表未来，不构成投资建议'
      : (sizeKey==='wide' ? '核心 4 进综合：股债利差 40% · 两融 25% · 波动率 20% · 资金面 15%；历史业绩不代表未来，不构成投资建议' : null);
    return '<div class="mx s-'+sizeKey+'">'+head(tt)
      +lb+(sizeKey==='wide' ? '<div class="g2">'+items+'</div>' : items)
      +foot(l2)
      +'</div>';
  }
  function buildInd(key, sizeKey){
    var v = M.indicators[key]; if(!v) return '';
    var o = ind(key);
    var txt = function(k, t){ return v[k] ? '<div class="blk"><div class="k">'+t+'</div><div class="v">'+v[k]+'</div></div>' : ''; };
    var left = '<div class="lc"><div class="nm" style="font-size:34px;font-weight:700;color:#5c564c">'+v.label+'</div>'
      +'<div class="big" style="color:'+o.hc+';margin-top:16px">'+(v.value==null?'—':v.value)+'<span class="u">'+(v.unit||'')+'</span></div>'
      +'<div class="trk"><i style="width:'+o.pct+'%;background:'+o.hc+'"></i></div>'
      +'<div class="pc" style="color:'+o.hc+'">近 5 年分位 '+(v.pct==null?'—':v.pct.toFixed(0)+'%')+'</div>'
      +'<div class="cc" style="color:'+o.hc+'">'+concl(key,v)+'</div></div>';
    var right = '<div class="rc">'
      + txt('desc','是什么') + txt('how','怎么算') + txt('range','常见范围')
      + txt('source','数据来源') + txt('caveat','口径局限')
      + '</div>';
    var body = sizeKey==='wide'
      ? '<div class="ind2">'+left+right+'</div>'
      : '<div>'+left.replace('class="lc"','class="lc" style="width:auto"')+'<div style="margin-top:30px">'+right+'</div></div>';
    return '<div class="mx s-'+sizeKey+'">'+head('市场情绪 · 单指标详解')+body+foot()+'</div>';
  }
  function buildCover(){
    var score=M.score, level=M.level;
    var chips = coreKeys().map(function(k){
      var v=M.indicators[k]; return v?shortChip(k,v):''; }).filter(Boolean).join(' · ');
    return '<div class="mx s-cover"><div class="cv-wrap">'
      +'<div class="cv-l"><div class="t1">市场情绪周报</div>'
      +'<div class="t2">4 个核心 + 4 个参考指标，一个分数看懂市场情绪<br>每周日 19:00 随周更更新</div>'
      +'<div class="t3">本期：'+chips+'</div></div>'
      +'<div class="cv-ring">'+ring(score, level, 'cover')+'</div>'
      +'</div></div>';
  }

  function build(name, sizeKey, key){
    ensureStyle();
    if (name==='market_score') return buildScore();                 /* 固定竖版 */
    if (name==='market_cover') return buildCover();                 /* 固定 1200×510 */
    if (name==='market_ind')   return buildInd(key||'erp', sizeKey==='xhs'?'xhs':'wide');
    if (name==='market_core' || name==='market_ref')
      return buildList(name, sizeKey==='wide'?'wide':'xhs');
    return '';
  }
  function hint(name, sizeKey){
    if (name==='market_score') return '情绪周榜大图（环形+一句话解读，1080×1620）';
    if (name==='market_cover') return '公众号封面（1200×510，2.35:1）';
    if (name==='market_ind')   return '单指标详解卡（'+(sizeKey==='xhs'?'竖版 1080×1620':'横版 1200×800')+'，&key=erp|margin|ivix|m1m2|pe|newfund|buffett|breaknet）';
    return (name==='market_ref'?'参考 4 详解':'核心 4 详解')+'（'+(sizeKey==='wide'?'公众号横版，含常见范围':'小红书竖版')+'）';
  }
  function nameOf(id){
    for (var i=0;i<BTN.length;i++) if (BTN[i][0]===id) return BTN[i][1];
    return id;
  }
  function buttons(){
    return BTN.map(function(b){ return '<button data-pool="'+b[0]+'">'+b[1]+'</button>'; }).join('');
  }

  window.MktExport = {
    available: !!(M && M.indicators),
    names: NAMES, sizes: SIZES,
    build: build, hint: hint, nameOf: nameOf, buttons: buttons,
    sizesOf: function(name){
      if (name==='market_score') return ['xhs'];
      if (name==='market_cover') return ['cover'];
      return ['xhs','wide'];
    }
  };
})();
