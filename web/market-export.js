/* 市场情绪导出图模块（2026-11-04 用户拍板：与 index.html 导出工作台同址）
   入口（index.html）：
     ?export=market_score &size=xhs            周榜大图：环形情绪分+一句话解读（1080×1620）
     ?export=market_core  &size=xhs|wide       核心 4 详解：小红书竖版 / 公众号横版（wide 含常见范围）
     ?export=market_ref   &size=xhs|wide       参考 4 详解：同上
     ?export=market_cover &size=cover          公众号封面（1200×510，2.35:1）
   全部加 &bare=1 走无头截图出图；数据随 data.js 每周日更新，属每周固定模板。
   （2026-11-05 用户拍板删除单指标卡 market_ind——已渲染的卡图仍留在期次包供发文。）
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
  var NAMES = ['market_score','market_core','market_ref','market_cover','market_weekly'];
  var BTN  = [['market_score','情绪大图'],['market_core','核心4详解'],['market_ref','参考4详解'],
              ['market_cover','情绪封面'],['market_weekly','市场周报']];

  /* ---- 市场周报合并图（2026-11-06 拍板）：上 1/4 情绪数值 + 下 3/4 大众指数温度 ----
     定位=引流图：只放大众叫得出名字的指数（小众的留在温度计详情），按分位升序
     （冷的在前，一眼看到谁便宜）；情绪区只给数值+少量信息。 */
  /* 23 只指数（2026-11-07 用户定单；按 code 匹配 payload，房地产实际码 CSI931775）
     四象限布局：左上=情绪，右上/左下/右下=估值三组（按分位升序 8/8/7），估值用列表行 */
  var MW_CODES = ['CSI932365','SZ399975','SZ399006','CSI931152','HKHSTECH','CSI000949','NDX',
    'CSI931775','CSI931139','CSI931151','SP500','SZ399997','SH000510','CSI931897','SH000300',
    'CSI930713','CSI931160','SZ399986','CSI931594','SH000688','H30590','CSI931865','SZ399998'];
  var MW_TC = {'极低估':['#e3f5ec','#0b6b46'],'低估':['#e9f6ee','#1a7f37'],
               '合理偏低':['#eef0f3','#4b5563'],'正常':['#eef0f3','#4b5563'],
               '合理偏高':['#fff3e3','#a85100'],'高估':['#fdecea','#b3261e']};
  function mwIndices(){
    var EQ = window.CARD_DATA && window.CARD_DATA.eq_index;
    var arr = (EQ && EQ.indices) || [];
    var by = {}; arr.forEach(function(x){ if (x.code) by[x.code] = x; });
    var out = [];
    MW_CODES.forEach(function(c){ if (by[c]) out.push(by[c]); });
    out.sort(function(a,b){
      return (a.percentile==null?999:a.percentile) - (b.percentile==null?999:b.percentile); });
    return out;
  }
  function mwRow(x, wide){
    var t = MW_TC[x.tier] || ['#eef0f3','#4b5563'];
    return '<div style="display:flex;align-items:center;gap:'+(wide?6:9)+'px;padding:'
      +(wide?'5px 2px':'8px 4px')+';border-bottom:1px solid #f0eae0;">'
      +'<span style="flex:1;min-width:0;font-size:'+(wide?13:16.5)+'px;font-weight:650;color:#26231e;'
      +'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'+x.name+'</span>'
      +'<span style="background:'+t[0]+';color:'+t[1]+';border-radius:999px;padding:1px '
      +(wide?'6':'8')+'px;font-size:'+(wide?9.5:11.5)+'px;font-weight:700;white-space:nowrap;">'+x.tier+'</span>'
      +'<b style="width:'+(wide?56:72)+'px;text-align:right;font-size:'+(wide?14:18)+'px;color:'+t[1]
      +';font-variant-numeric:tabular-nums;">'+(x.percentile==null?'—':x.percentile.toFixed(1)+'%')+'</b>'
      +'</div>';
  }
  function mwQuad(title, sub, inner, wide, tint){
    return '<div style="background:#ffffff;border-radius:14px;padding:'+(wide?'8px 10px':'12px 14px')
      +';min-width:0;min-height:0;display:flex;flex-direction:column;'
      +'box-shadow:0 2px 8px rgba(38,35,30,.05);">'
      +'<div style="display:flex;align-items:baseline;gap:6px;margin-bottom:'+(wide?3:6)+'px;">'
      +'<b style="font-size:'+(wide?13:16.5)+'px;color:'+tint+';">'+title+'</b>'
      +'<span style="font-size:'+(wide?9.5:12)+'px;color:#8a8378;">'+sub+'</span></div>'
      +'<div style="flex:1;min-height:0;overflow:hidden;">'+inner+'</div></div>';
  }
  function mwRowFill(x, wide, last, idx){
    var t = MW_TC[x.tier] || ['#eef0f3','#4b5563'];
    return '<div style="flex:1;display:flex;align-items:center;gap:'+(wide?8:12)+'px;'
      +'min-height:0;padding:'+(wide?'2px 4px':'4px 6px')+';'
      +(last?'':'border-bottom:1px solid #f1ebe1;')
      +(idx%2===1?'background:#fcfaf6;':'')+'">'
      +'<span style="flex:1;min-width:0;font-size:'+(wide?14:20)+'px;font-weight:700;color:#26231e;'
      +'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'+x.name+'</span>'
      +'<span style="background:'+t[0]+';color:'+t[1]+';border-radius:999px;padding:'+(wide?'2px 8px':'3px 12px')
      +';font-size:'+(wide?10.5:13.5)+'px;font-weight:700;white-space:nowrap;flex:0 0 auto;">'+x.tier+'</span>'
      +'<b style="width:'+(wide?62:84)+'px;text-align:right;font-size:'+(wide?16:24)+'px;color:'+t[1]
      +';font-variant-numeric:tabular-nums;flex:0 0 auto;">'
      +(x.percentile==null?'—':x.percentile.toFixed(1)+'%')+'</b>'
      +'</div>';
  }
  function mwRowV4(x, wide, last, idx){
    var t = MW_TC[x.tier] || ['#eef0f3','#4b5563'];
    return '<div style="flex:1;display:flex;align-items:center;gap:'+(wide?6:10)+'px;'
      +'min-height:0;padding:'+(wide?'1px 8px':'3px 12px')+';'
      +(last?'':'border-bottom:1px solid #f4efe6;')
      +(idx%2===1?'background:#fcfaf7;':'')+'">'
      +'<span style="flex:1;min-width:0;font-size:'+(wide?13.5:18)+'px;font-weight:700;color:#26231e;'
      +'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'+x.name+'</span>'
      +'<span style="background:'+t[0]+';color:'+t[1]+';border-radius:999px;padding:'+(wide?'1px 7px':'2px 10px')
      +';font-size:'+(wide?10:12.5)+'px;font-weight:700;white-space:nowrap;flex:0 0 auto;">'+x.tier+'</span>'
      +'<b style="width:'+(wide?56:78)+'px;text-align:right;font-size:'+(wide?14.5:21)+'px;color:'+t[1]
      +';font-variant-numeric:tabular-nums;flex:0 0 auto;">'
      +(x.percentile==null?'—':x.percentile.toFixed(1)+'%')+'</b>'
      +'</div>';
  }
  function mwRowV4(x, wide, last, idx){
    var t = MW_TC[x.tier] || ['#eef0f3','#4b5563'];
    return '<div style="flex:1;display:flex;align-items:center;gap:'+(wide?6:10)+'px;'
      +'min-height:0;padding:'+(wide?'1px 8px':'3px 12px')+';'
      +(last?'':'border-bottom:1px solid #f4efe6;')
      +(idx%2===1?'background:#fcfaf7;':'')+'">'
      +'<span style="flex:1;min-width:0;font-size:'+(wide?13.5:18.5)+'px;font-weight:700;color:#26231e;'
      +'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'+x.name+'</span>'
      /* 档位显眼化：徽章加宽加粗、分位数字放大 */
      +'<span style="background:'+t[0]+';color:'+t[1]+';border-radius:999px;padding:'+(wide?'2px 9px':'3px 13px')
      +';font-size:'+(wide?11:14)+'px;font-weight:800;white-space:nowrap;flex:0 0 auto;'
      +'box-shadow:inset 0 0 0 1.5px '+t[1]+'33;">'+x.tier+'</span>'
      +'<b style="width:'+(wide?62:88)+'px;text-align:right;font-size:'+(wide?16:25)+'px;font-weight:800;color:'
      +t[1]+';font-variant-numeric:tabular-nums;flex:0 0 auto;">'
      +(x.percentile==null?'—':x.percentile.toFixed(1)+'%')+'</b>'
      +'</div>';
  }
  function buildWeekly(sizeKey){
    var wide = sizeKey === 'wide';
    var score = M.score, level = M.level;
    var list = mwIndices();
    /* 按档位分三区（2026-11-07 修正：此前按位置切12/12 把低估混进热区）：
       低估=极低估+低估；正常=合理偏低+正常+合理偏高；高估=高估 */
    var isLow = function(x){ return x.tier==='极低估' || x.tier==='低估'; };
    var isHigh = function(x){ return x.tier==='高估'; };
    var lowL = list.filter(isLow);
    var midL = list.filter(function(x){ return !isLow(x) && !isHigh(x); });
    var highL = list.filter(isHigh);
    var hotNames = highL.slice(0,3).map(function(x){ return x.name.replace(/^中证/,''); }).join('、');
    /* 头条钩子 */
    var hook = '<div style="display:flex;align-items:flex-start;gap:'+(wide?20:32)+'px;">'
      +'<div style="flex:1;min-width:0;">'
      +'<div style="display:flex;align-items:baseline;gap:'+(wide?10:14)+'px;">'
      +'<b style="font-size:'+(wide?72:92)+'px;line-height:1;color:#0b6b46;font-variant-numeric:tabular-nums;">'
      + lowL.length + '</b>'
      +'<span style="font-size:'+(wide?28:38)+'px;font-weight:800;color:#26231e;">只还在便宜区</span></div>'
      +'<div style="font-size:'+(wide?14:19)+'px;color:#5c564c;margin-top:'+(wide?8:12)+'px;line-height:1.5;">'
      +'23 只大众指数 · '+highL.length+' 只已进高估（'+hotNames+'）</div></div>'
      +'<div class="sc-ring" style="width:'+(wide?160:196)+'px;height:'+(wide?160:196)+'px;'
      +'flex:0 0 auto;">'+ ring(score, level, 'quad') + '</div></div>';
    /* 情绪四数横条 */
    var strip = '<div style="display:flex;gap:'+(wide?8:12)+'px;background:#fff7ec;'
      +'border:1px solid #f3e2cd;border-radius:14px;padding:'+(wide?8:12)+'px;">'
      + coreKeys().map(function(k){
        var v = M.indicators[k]; if(!v) return '';
        return '<div style="flex:1;min-width:0;background:#ffffff;border-radius:10px;padding:'
          +(wide?'6px 8px':'10px 12px')+';">'
          +'<div style="font-size:'+(wide?10.5:13)+'px;color:#8a8378;white-space:nowrap;'
          +'overflow:hidden;text-overflow:ellipsis;">'+v.label+'</div>'
          +'<div style="display:flex;align-items:baseline;gap:4px;margin-top:2px;">'
          +'<b style="font-size:'+(wide?14:18)+'px;color:#26231e;font-variant-numeric:tabular-nums;">'
          + v.value + '<span style="font-size:'+(wide?9:11)+'px;color:#8a8378;font-weight:600;">'
          + (v.unit||'') + '</span></b>'
          +'<span style="margin-left:auto;font-size:'+(wide?10.5:13)+'px;font-weight:700;color:'
          + heatColor(v.pct)+';">'+(v.pct==null?'—':Math.round(v.pct)+'%')+'</span></div></div>';
      }).join('') + '</div>';
    /* 三区卡：标题带条数徽章；区域大小按条数弹性（左列低估 flex:13，右列 正常:flex:7 / 高估:flex:3） */
    var zone = function(title, icon, arr, tint, color, grow){
      var rows = arr.map(function(x,gi){ return mwRowV4(x, wide, gi===arr.length-1, gi); }).join('');
      return '<div style="flex:'+grow+' 1 0;background:#ffffff;border-radius:14px;overflow:hidden;'
        +'box-shadow:0 3px 12px rgba(38,35,30,.06);display:flex;flex-direction:column;min-height:0;">'
        +'<div style="display:flex;align-items:center;gap:9px;background:'+tint+';padding:'
        +(wide?7:12)+'px 14px;flex:0 0 auto;border-left:6px solid '+color+';">'
        +'<span style="font-size:'+(wide?16:22)+'px;">'+icon+'</span>'
        +'<b style="font-size:'+(wide?14:19)+'px;color:'+color+';letter-spacing:.5px;">'+title+'</b>'
        +'<span style="margin-left:auto;background:'+color+';color:#fff;border-radius:999px;'
        +'padding:2px '+(wide?9:13)+'px;font-size:'+(wide?11:15)+'px;font-weight:800;">'
        + arr.length + ' 只</span></div>'
        +'<div style="flex:1;display:flex;flex-direction:column;min-height:0;">'+rows+'</div></div>';
    };
    var lists = '<div style="flex:1;min-height:0;display:flex;gap:'+(wide?10:14)+'px;">'
      +'<div style="flex:13;min-width:0;display:flex;">'
      + zone('低估区 · 越低越便宜', '❄️', lowL, '#e9f6ee', '#0b6b46', 13) + '</div>'
      +'<div style="flex:10;min-width:0;display:flex;flex-direction:column;gap:'+(wide?8:12)+'px;">'
      + zone('正常区 · 不贵不便宜', '⛅', midL, '#eef0f3', '#475569', 7)
      + zone('高估区', '🔥', highL, '#fdecea', '#b3261e', 3)
      +'</div></div>';
    var bodyH = wide ? 556 : 1360;
    return '<div class="mx s-'+sizeKey+'">'+head('班马日记本 · 市场周报')
      +'<div style="height:'+bodyH+'px;display:flex;flex-direction:column;gap:'
      +(wide?8:14)+'px;">'
      + hook + strip + lists
      +'</div>'
      + foot('指数分位为历史自算估值参考；情绪分与分位均不构成投资建议')
      +'</div>';
  }

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
    var r = cls==='big' ? 176 : (cls==='cover' ? 132 : (cls==='quad' ? 132 : 120));
    var vb = cls==='big' ? 430 : (cls==='cover' ? 360 : 300);
    var sw = cls==='big' ? 34 : (cls==='cover' ? 26 : (cls==='quad' ? 26 : 26));
    var c = vb/2;
    var C = 2*Math.PI*r, fill = Math.max(0,Math.min(100,score))/100*C;
    var col = LV[level]||'#4b5563';
    var numSize = cls==='big' ? 132 : (cls==='cover' ? 110 : (cls==='quad' ? 60 : 92));
    var lvSize  = cls==='big' ? 40  : (cls==='cover' ? 34  : (cls==='quad' ? 20 : 30));
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
    /* 情绪图专属免责（2026-11-05 修：原误用 data.js 通用文案，里面写的是"心动值"） */
    var dis = '情绪分与各指标均为历史数据自算的分析参考，不构成任何投资建议或基金推介；'
      + '数据来自公开渠道，可能存在延迟与错误。历史业绩不代表未来，不构成投资建议。';
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
    if (name==='market_weekly') return buildWeekly(sizeKey==='wide'?'wide':'xhs');
    if (name==='market_core' || name==='market_ref')
      return buildList(name, sizeKey==='wide'?'wide':'xhs');
    return '';
  }
  function hint(name, sizeKey){
    if (name==='market_score') return '情绪周榜大图（环形+一句话解读，1080×1620）';
    if (name==='market_cover') return '公众号封面（1200×510，2.35:1）';
    if (name==='market_weekly') return '市场周报合并图（上1/4情绪数值 + 下3/4大众指数温度'
      + (sizeKey==='wide'?'，横版）':'，竖版）');
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
