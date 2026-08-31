# -*- coding: utf-8 -*-
"""生成单文件自包含 HTML 画像报告。

设计约束：
  * **完全离线**：不引任何 CDN，图表全部用原生 JS 画 SVG，双击即可打开；
  * **数据内嵌**：统计结果以 JSON 注入，无需后端；
  * **可交互**：明暗主题切换、明细表搜索/排序/分页、前端导出 CSV。
"""

from __future__ import annotations

import html as _html
import json
import os
from typing import Any, Dict, List, Optional

from . import APP_NAME, __version__

_CSS = """
:root{
  --bg:#0f1115; --bg2:#151922; --card:#1a1f2b; --card2:#202634;
  --line:#2a3242; --tx:#e6ebf5; --tx2:#9aa7bd; --tx3:#6b7893;
  --ac:#4f8cff; --ac2:#17c9a5; --ac3:#ff8f4f; --ac4:#c47bff; --warn:#ff5f6d;
  --grad1:linear-gradient(135deg,#4f8cff,#17c9a5);
}
html[data-theme="light"]{
  --bg:#f5f7fb; --bg2:#ffffff; --card:#ffffff; --card2:#f0f3f9;
  --line:#dfe5f0; --tx:#1b2333; --tx2:#5a6883; --tx3:#8b98b0;
  --grad1:linear-gradient(135deg,#2f6bff,#0fa98a);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:14px;line-height:1.6}
a{color:var(--ac);text-decoration:none}
.wrap{max-width:1360px;margin:0 auto;padding:0 22px 80px}
header.hero{background:var(--grad1);padding:34px 0 30px;margin-bottom:26px;
  box-shadow:0 6px 30px rgba(0,0,0,.28)}
header.hero .wrap{padding-bottom:0}
h1{margin:0 0 6px;font-size:26px;font-weight:700;color:#fff;letter-spacing:.5px}
.sub{color:rgba(255,255,255,.86);font-size:13px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.btn{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.28);color:#fff;
  padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px;transition:.15s}
.btn:hover{background:rgba(255,255,255,.28)}
html[data-theme="light"] .btn{background:rgba(0,0,0,.06);border-color:rgba(0,0,0,.15);color:var(--tx)}
section{margin-bottom:34px;scroll-margin-top:70px}
h2{font-size:19px;margin:0 0 4px;display:flex;align-items:center;gap:9px}
h2::before{content:"";width:4px;height:19px;background:var(--grad1);border-radius:2px}
.hint{color:var(--tx2);font-size:12.5px;margin:0 0 14px}
.grid{display:grid;gap:16px}
.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
.g4{grid-template-columns:repeat(4,minmax(0,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card h3{margin:0 0 12px;font-size:14.5px;color:var(--tx);font-weight:600;
  display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.card h3 small{color:var(--tx3);font-weight:400;font-size:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px 17px}
.kpi .v{font-size:25px;font-weight:700;letter-spacing:-.5px;line-height:1.25}
.kpi .k{color:var(--tx2);font-size:12.5px;margin-top:2px}
.kpi .u{font-size:13px;color:var(--tx3);font-weight:500;margin-left:3px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--card2);border:1px solid var(--line);border-radius:999px;
  padding:5px 13px;font-size:13px}
.chip b{color:var(--ac2);margin-left:5px;font-weight:600}
.chip.t1{background:rgba(79,140,255,.14);border-color:rgba(79,140,255,.4)}
.chip.t2{background:rgba(23,201,165,.13);border-color:rgba(23,201,165,.36)}
.chip.t3{background:rgba(196,123,255,.13);border-color:rgba(196,123,255,.36)}
.summary{background:var(--card2);border-left:3px solid var(--ac);border-radius:8px;
  padding:13px 16px;font-size:14px;line-height:1.85}
.bar-row{display:grid;grid-template-columns:120px 1fr 78px;gap:10px;align-items:center;
  margin-bottom:7px;font-size:13px}
.bar-row .lb{text-align:right;color:var(--tx2);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.bar-track{background:var(--card2);border-radius:5px;height:17px;overflow:hidden}
.bar-fill{height:100%;border-radius:5px;background:var(--grad1);transition:width .5s}
.bar-fill.alt{background:linear-gradient(135deg,#ff8f4f,#c47bff)}
.bar-row .vl{color:var(--tx2);font-size:12.5px;text-align:left}
.cloud{display:flex;flex-wrap:wrap;gap:7px 12px;align-items:baseline;line-height:1.9}
.cloud span{transition:.15s}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--tx2);font-weight:600;font-size:12.5px;position:sticky;top:0;
  background:var(--card);cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--ac)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.scroll{max-height:460px;overflow:auto;border-radius:8px}
.tbl-wrap{border:1px solid var(--line);border-radius:10px;overflow:hidden}
input.search{width:100%;background:var(--card2);border:1px solid var(--line);
  color:var(--tx);border-radius:8px;padding:9px 12px;font-size:13px;margin-bottom:10px}
input.search:focus{outline:none;border-color:var(--ac)}
.pager{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:10px;
  color:var(--tx2);font-size:12.5px}
.pager button{background:var(--card2);border:1px solid var(--line);color:var(--tx);
  border-radius:6px;padding:4px 11px;cursor:pointer;font-size:12.5px}
.pager button:disabled{opacity:.4;cursor:not-allowed}
svg text{fill:var(--tx2);font-size:11px}
svg .gl{stroke:var(--line);stroke-width:1;stroke-dasharray:3 4;opacity:.55}
.empty{color:var(--tx3);text-align:center;padding:26px 0;font-size:13px}
/* 悬浮说明（hover tooltip） */
.tip{position:relative;display:inline-flex;align-items:center;justify-content:center;
  width:17px;height:17px;border-radius:50%;background:var(--ac);color:#fff;font-size:11px;
  font-weight:700;cursor:help;margin-left:8px;vertical-align:middle;flex:0 0 auto;
  box-shadow:0 0 0 3px rgba(79,140,255,.18)}
.tip:hover{background:#3a78f0}
.tip .tiptext{visibility:hidden;opacity:0;position:absolute;left:50%;bottom:150%;
  transform:translateX(-50%) translateY(4px);width:300px;background:#0b0e14;color:var(--tx);
  border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:12.5px;font-weight:400;
  line-height:1.7;text-align:left;z-index:60;transition:opacity .15s,transform .15s;
  box-shadow:0 12px 40px rgba(0,0,0,.5);pointer-events:none}
.tip .tiptext b{color:var(--ac2);font-weight:600}
.tip .tiptext::after{content:"";position:absolute;top:100%;left:50%;
  transform:translateX(-50%);border:7px solid transparent;border-top-color:#0b0e14}
.tip:hover .tiptext{visibility:visible;opacity:1;transform:translateX(-50%) translateY(0)}
footer{color:var(--tx3);font-size:12px;text-align:center;padding:26px 0 0;
  border-top:1px solid var(--line);margin-top:30px}
@media(max-width:1000px){.g3,.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:640px){.g2,.g3,.g4{grid-template-columns:1fr}}
"""

_JS = r"""
const D = window.__DATA__;
const $ = s => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if(c) e.className = c;
  if(h != null) e.innerHTML = h; return e; };
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = n => (n == null || n === '' ? '' : Number(n).toLocaleString('zh-CN'));

function setTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  try{ localStorage.setItem('nfo-theme', t); }catch(e){}
  renderAll();
}
(function(){ let t = 'dark';
  try{ t = localStorage.getItem('nfo-theme') || 'dark'; }catch(e){}
  document.documentElement.setAttribute('data-theme', t); })();

function kpi(k, v, u){
  return '<div class="kpi"><div class="v">' + v + (u ? '<span class="u">'+u+'</span>' : '') +
         '</div><div class="k">' + esc(k) + '</div></div>';
}

/* 横向条形图 */
function barH(node, items, opts){
  opts = opts || {}; node.innerHTML = '';
  if(!items || !items.length){ node.appendChild(el('div','empty','暂无数据')); return; }
  const max = Math.max.apply(null, items.map(i => i.count)) || 1;
  items.slice(0, opts.limit || 100).forEach(it => {
    const row = el('div','bar-row');
    row.appendChild(el('div','lb', esc(it.name)));
    const tr = el('div','bar-track');
    const fl = el('div','bar-fill' + (opts.alt ? ' alt' : ''));
    fl.style.width = Math.max(1.5, it.count / max * 100) + '%';
    tr.appendChild(fl); row.appendChild(tr);
    row.appendChild(el('div','vl', fmt(it.count) + (it.pct ? ' (' + it.pct + '%)' : '')));
    node.appendChild(row);
  });
}

/* 环形图 */
function donut(node, items, opts){
  opts = opts || {}; node.innerHTML = '';
  const data = (items || []).filter(i => i.count > 0);
  const total = data.reduce((s,i) => s + i.count, 0);
  if(!total){ node.appendChild(el('div','empty','暂无数据')); return; }
  const R = 78, r = 48, cx = 95, cy = 95;
  const colors = ['#4f8cff','#17c9a5','#ff8f4f','#c47bff','#ffd166','#ff5f6d','#5ad1ff','#8bd450'];
  let svg = '<svg viewBox="0 0 190 190" width="100%" height="190">';
  let ang = -Math.PI / 2;
  if(data.length === 1){
    svg += '<circle cx="'+cx+'" cy="'+cy+'" r="'+((R+r)/2)+'" fill="none" stroke="'+colors[0]+
           '" stroke-width="'+(R-r)+'"/>';
  } else {
    data.forEach((d, i) => {
      const a2 = ang + d.count / total * Math.PI * 2;
      const big = (a2 - ang) > Math.PI ? 1 : 0;
      const x1 = cx + R*Math.cos(ang), y1 = cy + R*Math.sin(ang);
      const x2 = cx + R*Math.cos(a2),  y2 = cy + R*Math.sin(a2);
      const x3 = cx + r*Math.cos(a2),  y3 = cy + r*Math.sin(a2);
      const x4 = cx + r*Math.cos(ang), y4 = cy + r*Math.sin(ang);
      svg += '<path d="M'+x1.toFixed(1)+' '+y1.toFixed(1)+' A'+R+' '+R+' 0 '+big+' 1 '+
             x2.toFixed(1)+' '+y2.toFixed(1)+' L'+x3.toFixed(1)+' '+y3.toFixed(1)+' A'+r+' '+r+
             ' 0 '+big+' 0 '+x4.toFixed(1)+' '+y4.toFixed(1)+' Z" fill="'+colors[i%colors.length]+
             '" opacity=".92"><title>'+esc(d.name)+': '+fmt(d.count)+'</title></path>';
      ang = a2;
    });
  }
  svg += '<text x="'+cx+'" y="'+(cy-3)+'" text-anchor="middle" style="font-size:19px;fill:var(--tx);font-weight:700">'+
         fmt(total)+'</text>';
  svg += '<text x="'+cx+'" y="'+(cy+15)+'" text-anchor="middle" style="font-size:11px">'+
         esc(opts.center || '总数')+'</text></svg>';
  node.appendChild(el('div','', svg));
  const lg = el('div','chips'); lg.style.marginTop = '8px';
  data.forEach((d, i) => {
    const p = (d.count / total * 100).toFixed(1);
    lg.appendChild(el('span','chip',
      '<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:'+
      colors[i%colors.length]+';margin-right:6px"></span>'+esc(d.name)+' <b>'+p+'%</b>'));
  });
  node.appendChild(lg);
}

/* 柱状图 */
function columns(node, items, opts){
  opts = opts || {}; node.innerHTML = '';
  const data = (items || []).filter(i => opts.keepZero || i.count > 0);
  if(!data.length){ node.appendChild(el('div','empty','暂无数据')); return; }
  const W = Math.max(320, node.clientWidth || 560), H = opts.h || 200;
  const pad = {l: 42, r: 10, t: 12, b: 32};
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const max = Math.max.apply(null, data.map(d => d.count)) || 1;
  let svg = '<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="'+H+'">';
  for(let g = 0; g <= 4; g++){
    const y = pad.t + ih - ih*g/4;
    svg += '<line class="gl" x1="'+pad.l+'" y1="'+y.toFixed(1)+'" x2="'+(W-pad.r)+'" y2="'+y.toFixed(1)+'"/>'+
           '<text x="'+(pad.l-6)+'" y="'+(y+4).toFixed(1)+'" text-anchor="end">'+fmt(Math.round(max*g/4))+'</text>';
  }
  const bw = iw / data.length;
  data.forEach((d, i) => {
    const h = Math.max(1, d.count / max * ih);
    const x = pad.l + i*bw + bw*0.16, w = bw*0.68, y = pad.t + ih - h;
    svg += '<rect x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+w.toFixed(1)+'" height="'+h.toFixed(1)+
           '" rx="3" fill="url(#g1)"><title>'+esc(d.name)+': '+fmt(d.count)+'</title></rect>';
    const step = Math.ceil(data.length / (opts.maxLabels || 14));
    if(i % step === 0 || i === data.length-1){
      svg += '<text x="'+(x+w/2).toFixed(1)+'" y="'+(H-10)+'" text-anchor="middle">'+esc(d.name)+'</text>';
    }
  });
  svg += '<defs><linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">'+
         '<stop offset="0%" stop-color="#4f8cff"/><stop offset="100%" stop-color="#17c9a5"/>'+
         '</linearGradient></defs></svg>';
  node.appendChild(el('div','', svg));
}

/* 折线面积图 */
function line(node, items, opts){
  opts = opts || {}; node.innerHTML = '';
  const data = items || [];
  if(data.length < 2){ node.appendChild(el('div','empty','数据点不足')); return; }
  const W = Math.max(340, node.clientWidth || 560), H = opts.h || 200;
  const pad = {l: 46, r: 12, t: 12, b: 32};
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const max = Math.max.apply(null, data.map(d => d.count)) || 1;
  let svg = '<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="'+H+'">';
  for(let g = 0; g <= 4; g++){
    const y = pad.t + ih - ih*g/4;
    svg += '<line class="gl" x1="'+pad.l+'" y1="'+y.toFixed(1)+'" x2="'+(W-pad.r)+'" y2="'+y.toFixed(1)+'"/>'+
           '<text x="'+(pad.l-6)+'" y="'+(y+4).toFixed(1)+'" text-anchor="end">'+fmt(Math.round(max*g/4))+'</text>';
  }
  const pts = data.map((d,i) => [pad.l + iw*i/(data.length-1), pad.t + ih - d.count/max*ih]);
  const path = pts.map((p,i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  svg += '<defs><linearGradient id="ga" x1="0" y1="0" x2="0" y2="1">'+
         '<stop offset="0%" stop-color="#4f8cff" stop-opacity=".38"/>'+
         '<stop offset="100%" stop-color="#4f8cff" stop-opacity="0"/></linearGradient></defs>';
  svg += '<path d="'+path+' L'+pts[pts.length-1][0].toFixed(1)+' '+(pad.t+ih)+' L'+pts[0][0].toFixed(1)+
         ' '+(pad.t+ih)+' Z" fill="url(#ga)"/>';
  svg += '<path d="'+path+'" fill="none" stroke="#4f8cff" stroke-width="2.2" stroke-linejoin="round"/>';
  pts.forEach((p,i) => { svg += '<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+
    '" r="2.6" fill="#4f8cff"><title>'+esc(data[i].name)+': '+fmt(data[i].count)+'</title></circle>'; });
  const step = Math.ceil(data.length / 12);
  data.forEach((d,i) => { if(i % step === 0 || i === data.length-1){
    svg += '<text x="'+pts[i][0].toFixed(1)+'" y="'+(H-10)+'" text-anchor="middle">'+esc(d.name)+'</text>'; }});
  svg += '</svg>';
  node.appendChild(el('div','', svg));
}

/* 雷达图 */
function radar(node, axes){
  node.innerHTML = '';
  if(!axes || !axes.length){ node.appendChild(el('div','empty','暂无数据')); return; }
  const S = 310, cx = S/2, cy = S/2 + 2, R = 98, n = axes.length;
  let svg = '<svg viewBox="0 0 '+S+' '+S+'" width="100%" height="'+S+'">';
  for(let ring = 1; ring <= 4; ring++){
    const pts = [];
    for(let i = 0; i < n; i++){
      const a = -Math.PI/2 + i*2*Math.PI/n, rr = R*ring/4;
      pts.push((cx+rr*Math.cos(a)).toFixed(1)+','+(cy+rr*Math.sin(a)).toFixed(1));
    }
    svg += '<polygon points="'+pts.join(' ')+'" fill="none" stroke="var(--line)" stroke-width="1" opacity=".7"/>';
  }
  for(let i = 0; i < n; i++){
    const a = -Math.PI/2 + i*2*Math.PI/n;
    svg += '<line x1="'+cx+'" y1="'+cy+'" x2="'+(cx+R*Math.cos(a)).toFixed(1)+'" y2="'+
           (cy+R*Math.sin(a)).toFixed(1)+'" stroke="var(--line)" stroke-width="1"/>';
  }
  const pts = axes.map((d,i) => {
    const a = -Math.PI/2 + i*2*Math.PI/n, rr = R*Math.max(2,d.value)/100;
    return [cx+rr*Math.cos(a), cy+rr*Math.sin(a)];
  });
  svg += '<polygon points="'+pts.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ')+
         '" fill="rgba(79,140,255,.28)" stroke="#4f8cff" stroke-width="2"/>';
  pts.forEach((p, i) => { svg += '<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+
    '" r="3.2" fill="#4f8cff"><title>'+esc(axes[i].name)+': '+
    axes[i].value.toFixed(0)+'</title></circle>'; });
  axes.forEach((d,i) => {
    const a = -Math.PI/2 + i*2*Math.PI/n, rr = R + 22;
    const x = cx+rr*Math.cos(a), y = cy+rr*Math.sin(a);
    const an = Math.abs(Math.cos(a)) < .3 ? 'middle' : (Math.cos(a) > 0 ? 'start' : 'end');
    svg += '<text x="'+x.toFixed(1)+'" y="'+(y+3).toFixed(1)+'" text-anchor="'+an+
           '" style="font-size:11.5px;fill:var(--tx)">'+esc(d.name)+'</text>';
    svg += '<text x="'+x.toFixed(1)+'" y="'+(y+15).toFixed(1)+'" text-anchor="'+an+
           '" style="font-size:10.5px;fill:var(--tx3)">'+d.value.toFixed(0)+'</text>';
  });
  svg += '</svg>';
  node.appendChild(el('div','', svg));
  const tips = el('div','chips'); tips.style.marginTop = '6px';
  axes.forEach(d => { if(d.hint) tips.appendChild(el('span','chip', esc(d.name)+'：'+esc(d.hint))); });
  if(tips.children.length) node.appendChild(tips);
}

/* 热力矩阵 */
function heatmap(node, labels, matrix){
  node.innerHTML = '';
  if(!labels || labels.length < 2){ node.appendChild(el('div','empty','数据不足')); return; }
  const n = labels.length, cell = 22, padL = 96, padT = 84;
  const W = padL + n*cell + 12, H = padT + n*cell + 12;
  let max = 0;
  matrix.forEach(row => row.forEach(v => { if(v > max) max = v; }));
  let svg = '<svg viewBox="0 0 '+W+' '+H+'" width="100%" style="max-height:660px">';
  for(let i = 0; i < n; i++){
    svg += '<text x="'+(padL-6)+'" y="'+(padT+i*cell+cell/2+4)+'" text-anchor="end" style="font-size:10.5px">'+
           esc(labels[i].slice(0,7))+'</text>';
    svg += '<text transform="translate('+(padL+i*cell+cell/2)+','+(padT-6)+') rotate(-58)" style="font-size:10.5px">'+
           esc(labels[i].slice(0,7))+'</text>';
    for(let j = 0; j < n; j++){
      const v = (matrix[i] && matrix[i][j]) || 0;
      const op = v ? (0.12 + 0.82 * Math.pow(v/max, 0.55)) : 0.05;
      svg += '<rect x="'+(padL+j*cell)+'" y="'+(padT+i*cell)+'" width="'+(cell-1.5)+'" height="'+(cell-1.5)+
             '" rx="2" fill="'+(i===j ? '#ff8f4f' : '#4f8cff')+'" opacity="'+op.toFixed(3)+'"><title>'+
             esc(labels[i])+' × '+esc(labels[j])+': '+fmt(v)+'</title></rect>';
    }
  }
  svg += '</svg>';
  node.appendChild(el('div','', svg));
}

/* 词云 */
function cloud(node, items, opts){
  opts = opts || {}; node.innerHTML = '';
  const data = (items || []).filter(i => i.count > 0);
  if(!data.length){ node.appendChild(el('div','empty','暂无数据')); return; }
  const max = Math.max.apply(null, data.map(d => d.count)) || 1;
  const min = Math.min.apply(null, data.map(d => d.count)) || 0;
  const box = el('div','cloud');
  data.slice(0, opts.limit || 80).forEach(d => {
    const t = (d.count - min) / Math.max(1, (max - min));
    const size = (12 + t * 20).toFixed(1);
    const hue = 205 + t * 110;
    const sp = el('span','', esc(d.name));
    sp.style.fontSize = size + 'px';
    sp.style.color = 'hsl('+hue.toFixed(0)+' 72% '+(58 + t*12).toFixed(0)+'%)';
    sp.style.fontWeight = t > .55 ? '600' : '400';
    sp.title = d.name + ': ' + fmt(d.count);
    box.appendChild(sp);
  });
  node.appendChild(box);
}

/* 明细表：搜索 + 排序 + 分页 */
function renderTable(cols, rows, mount){
  const st = {sort: null, dir: 1, page: 0, size: 20, q: ''};
  const wrap = $(mount); wrap.innerHTML = '';
  const inp = el('input','search');
  inp.placeholder = '搜索…（支持任意列关键字）';
  wrap.appendChild(inp);
  const box = el('div','tbl-wrap'), sc = el('div','scroll'), tb = el('table');
  const thead = el('thead'), tr = el('tr');
  cols.forEach(c => {
    const th = el('th', c.num ? 'num' : '', esc(c.label));
    th.onclick = () => { if(st.sort === c.key) st.dir *= -1; else { st.sort = c.key; st.dir = -1; } draw(); };
    tr.appendChild(th);
  });
  thead.appendChild(tr); tb.appendChild(thead);
  const tbody = el('tbody'); tb.appendChild(tbody);
  sc.appendChild(tb); box.appendChild(sc); wrap.appendChild(box);
  const pager = el('div','pager'); wrap.appendChild(pager);

  function filtered(){
    const q = st.q.trim().toLowerCase();
    let rs = rows;
    if(q) rs = rs.filter(r => cols.some(c => String(r[c.key] == null ? '' : r[c.key]).toLowerCase().indexOf(q) >= 0));
    if(st.sort){
      const k = st.sort;
      rs = rs.slice().sort((a,b) => {
        const va = a[k], vb = b[k];
        if(typeof va === 'number' && typeof vb === 'number') return (va - vb) * st.dir;
        return String(va).localeCompare(String(vb), 'zh-CN') * st.dir;
      });
    }
    return rs;
  }
  function draw(){
    const rs = filtered();
    const pages = Math.max(1, Math.ceil(rs.length / st.size));
    if(st.page >= pages) st.page = pages - 1;
    if(st.page < 0) st.page = 0;
    const slice = rs.slice(st.page*st.size, (st.page+1)*st.size);
    tbody.innerHTML = slice.length
      ? slice.map(r => '<tr>' + cols.map(c =>
          '<td class="'+(c.num?'num':'')+'">'+esc(r[c.key])+'</td>').join('') + '</tr>').join('')
      : '<tr><td colspan="'+cols.length+'" class="empty">没有匹配的记录</td></tr>';
    pager.innerHTML = '共 ' + fmt(rs.length) + ' 条　';
    const mk = (label, dis, fn) => { const b = el('button','',label);
      b.disabled = dis; b.onclick = fn; pager.appendChild(b); };
    mk('首页', st.page === 0, () => { st.page = 0; draw(); });
    mk('上一页', st.page === 0, () => { st.page--; draw(); });
    pager.appendChild(el('span','', ' ' + (st.page+1) + ' / ' + pages + ' '));
    mk('下一页', st.page >= pages-1, () => { st.page++; draw(); });
    mk('末页', st.page >= pages-1, () => { st.page = pages-1; draw(); });
  }
  inp.oninput = e => { st.q = e.target.value; st.page = 0; draw(); };
  draw();
}

/* 前端导出 */
function toCSV(rows, cols){
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g,'""') + '"';
  return '\ufeff' + cols.map(c => q(c.label)).join(',') + '\n' +
         rows.map(r => cols.map(c => q(r[c.key])).join(',')).join('\n');
}
function download(name, text){
  const blob = new Blob([text], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
}
window.__exportView = function(key){
  const m = {tags:['高频标签','tags'], actors:['高频艺人','actors'], studios:['片商','studios'],
             series:['系列','series'], directors:['导演','directors'], keywords:['剧情高频词','keywords']};
  const cfg = m[key] || ['数据', key];
  const rows = (D[cfg[1]] || []).map((x,i) => ({rank:i+1, name:x.name, count:x.count}));
  download(cfg[0] + '.csv', toCSV(rows, [{label:'排名',key:'rank'},{label:cfg[0],key:'name'},{label:'出现次数',key:'count'}]));
};

function renderAll(){
  const P = D.portrait || {}, O = D.overview || {};
  if(!O.total){
    $('#app').innerHTML = '<div class="card"><div class="empty">数据库中没有记录，请先扫描 NFO 目录。</div></div>';
    return;
  }
  const dist = O.distinct || {};

  document.getElementById('kpis').innerHTML = [
    kpi('收录作品', fmt(O.total), '部'),
    kpi('涉及艺人', fmt(dist.actors), '位'),
    kpi('内容标签', fmt(dist.tags), '个'),
    kpi('片商', fmt(dist.studios), '家'),
    kpi('总时长', fmt(O.total_hours), '小时'),
    kpi('平均时长', O.avg_runtime, '分钟'),
    kpi('平均评分', O.avg_rating, '分'),
    kpi('年份跨度', (O.year_min || '-') + '–' + (O.year_max || '-'), ''),
  ].join('');

  $('#p-summary').textContent = P.summary || '';
  radar($('#p-radar'), P.radar);
  const mk = (arr, cls) => (arr || []).map(x =>
    '<span class="chip '+cls+'">'+esc(x.name)+'<b>'+fmt(x.count)+'</b></span>').join('');
  $('#p-tags').innerHTML = mk(P.favorite_tags, 't1');
  $('#p-actors').innerHTML = mk(P.favorite_actors, 't2');
  $('#p-studios').innerHTML = mk(P.favorite_studios, 't3');

  barH($('#c-tags'), (D.tags||[]).map(t => ({name:t.name, count:t.count,
       pct:(t.count/O.total*100).toFixed(1)})));
  cloud($('#c-cloud'), D.tags, {limit: 70});
  barH($('#c-tech'), D.tech_tags, {alt:true});

  barH($('#c-actors'), D.actors);
  renderTable([
    {label:'艺人', key:'name'},
    {label:'作品数', key:'count', num:true},
    {label:'平均评分', key:'avg_rating', num:true},
    {label:'活跃年份', key:'years'},
    {label:'平均时长', key:'avg_runtime', num:true},
    {label:'已看', key:'watched', num:true},
    {label:'代表片商', key:'studio'},
    {label:'代表标签', key:'tag'},
  ], (D.actors||[]).map(a => ({
    name:a.name, count:a.count, avg_rating:a.avg_rating,
    years:(a.year_from || '?') + '–' + (a.year_to || '?'),
    avg_runtime:a.avg_runtime, watched:a.watched,
    studio:(a.top_studios||[]).map(s => s.name).join('、'),
    tag:(a.top_tags||[]).slice(0,4).map(t => t.name).join('、'),
  })), '#t-actors');

  $('#t-copair').innerHTML = (D.actor_pairs || []).map((p,i) =>
    '<tr><td class="num">'+(i+1)+'</td><td>'+esc(p.a)+'</td><td>'+esc(p.b)+
    '</td><td class="num">'+fmt(p.count)+'</td></tr>').join('') ||
    '<tr><td colspan="4" class="empty">暂无共演数据</td></tr>';
  donut($('#c-acount'), D.dist_actor_count, {center:'作品'});

  barH($('#c-studios'), D.studios);
  barH($('#c-series'), D.series, {alt:true});
  barH($('#c-directors'), D.directors);
  barH($('#c-prefixes'), D.prefixes, {alt:true});

  donut($('#c-res'), D.dist_resolution, {center:'作品'});
  donut($('#c-censor'), D.dist_censor, {center:'作品'});
  $('#c-censor-tip').innerHTML = (D.dist_censor||[]).map(c =>
    '<span class="chip">'+esc(c.name)+'<b>'+fmt(c.count)+'</b></span>').join('');

  line($('#c-year'), D.dist_year);
  line($('#c-month'), D.dist_added_month, {h:190});
  columns($('#c-hour'), D.dist_added_hour, {h:190, keepZero:true});
  columns($('#c-week'), D.dist_weekday, {h:190});

  columns($('#c-rating'), D.dist_rating, {h:200, keepZero:true});
  columns($('#c-runtime'), D.dist_runtime, {h:200});

  if(D.cooccurrence){
    heatmap($('#c-heat'), D.cooccurrence.labels, D.cooccurrence.matrix);
    $('#t-pairs').innerHTML = (D.cooccurrence.edges || []).slice(0,25).map((e,i) =>
      '<tr><td class="num">'+(i+1)+'</td><td>'+esc(e.source)+'</td><td>'+esc(e.target)+
      '</td><td class="num">'+fmt(e.value)+'</td></tr>').join('') ||
      '<tr><td colspan="4" class="empty">暂无共现数据</td></tr>';
  }
  cloud($('#c-kwcloud'), D.keywords, {limit:60});
  barH($('#c-kwbar'), D.keywords, {alt:true});

  renderTable([
    {label:'番号', key:'num'}, {label:'标题', key:'title'},
    {label:'年份', key:'year', num:true}, {label:'发行', key:'premiered'},
    {label:'片商', key:'studio'}, {label:'导演', key:'director'},
    {label:'演员', key:'actors'}, {label:'标签', key:'tags'},
    {label:'评分', key:'userrating', num:true}, {label:'时长', key:'runtime_min', num:true},
    {label:'画质', key:'resolution'},
  ], D.movies || [], '#t-movies');
}

let __rt = null;
window.addEventListener('resize', () => { clearTimeout(__rt); __rt = setTimeout(renderAll, 260); });
renderAll();
"""


def _json_for_js(obj: Any) -> str:
    """序列化为可安全嵌入 <script> 的 JSON。"""
    return (
        json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body>
<header class="hero">
  <div class="wrap">
    <h1>__TITLE__</h1>
    <div class="sub">__SUB__</div>
    <div class="toolbar">
      <button class="btn" onclick="setTheme(document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark')">切换明暗主题</button>
      <button class="btn" onclick="window.__exportView('tags')">导出高频标签 CSV</button>
      <button class="btn" onclick="window.__exportView('actors')">导出高频艺人 CSV</button>
      <button class="btn" onclick="window.__exportView('keywords')">导出剧情高频词 CSV</button>
    </div>
  </div>
</header>

<div class="wrap" id="app">
  <section id="s-overview">
    <h2>总览</h2>
    <p class="hint">基于全部 __TOTAL__ 条 NFO 记录统计</p>
    <div class="grid g4" id="kpis"></div>
  </section>

  <section id="s-portrait">
    <h2>用户画像</h2>
    <p class="hint">一句话总结与八维偏好雷达</p>
    <div class="card" style="margin-bottom:16px"><div class="summary" id="p-summary"></div></div>
    <div class="grid g2">
      <div class="card"><h3>偏好雷达 <small>0-100 强度</small></h3><div id="p-radar"></div></div>
      <div class="card">
        <h3>偏好标签 <small>Top 12</small></h3><div class="chips" id="p-tags"></div>
        <h3 style="margin-top:18px">偏好艺人 <small>Top 10</small></h3><div class="chips" id="p-actors"></div>
        <h3 style="margin-top:18px">偏好片商 <small>Top 6</small></h3><div class="chips" id="p-studios"></div>
      </div>
    </div>
  </section>

  <section id="s-tags">
    <h2>高频标签<span class="tip">i<span class="tiptext">高频标签：统计全部作品中出现的「内容主题标签」Top 排名。已自动剥离片商 / 系列 / 导演 / 演员名 / 画质编码等元数据，仅保留剧情与类型主题。</span></span></h2>
    <p class="hint">已剥离「系列 / 片商 / 发行 / 导演 / 画质 / 编码 / 番号 / 演员名」等元数据噪音，只保留真正的内容标签</p>
    <div class="grid g2">
      <div class="card"><h3>标签频次 Top __TOP_TAGS__ <small>占比 = 含该标签的作品占比</small><span class="tip">i<span class="tiptext">标签频次 Top <b>__TOP_TAGS__</b>：按「含有该标签的作品数量」从高到低排序。右侧百分比 = 含此标签的作品数 ÷ 全部作品数。标签已剥离元数据噪音，仅保留内容主题；同义标签已合并。</span></span></h3><div id="c-tags"></div></div>
      <div class="card"><h3>标签云 <small>字号随频次放大</small></h3><div id="c-cloud"></div></div>
    </div>
    <div class="card" style="margin-top:16px">
      <h3>技术标签分布 <small>画质 / 编码，不计入内容标签</small></h3><div id="c-tech"></div>
    </div>
  </section>

  <section id="s-actors">
    <h2>高频艺人<span class="tip">i<span class="tiptext">高频艺人：按出演作品数排名的常驻演员。异写（如 響蓮 / 响莲、日文汉字与简体）已自动归并为同一人。</span></span></h2>
    <p class="hint">繁简与日文异体字已自动归并（如 響蓮 / 响莲 计为同一人）；表头可点击排序，支持搜索</p>
    <div class="grid g2">
      <div class="card"><h3>作品数 Top __TOP_ACTORS__<span class="tip">i<span class="tiptext">作品数 Top <b>__TOP_ACTORS__</b>：按出演作品数排序。同名异写已归并；平均评分、活跃年份、代表片商 / 标签来自全量统计。右侧明细表头可点击排序，支持搜索。</span></span></h3><div id="c-actors"></div></div>
      <div class="card"><h3>艺人明细 <small>含代表片商与代表标签</small></h3><div id="t-actors"></div></div>
    </div>
    <div class="grid g2" style="margin-top:16px">
      <div class="card"><h3>高频共演组合</h3>
        <div class="scroll" style="max-height:300px">
          <table><thead><tr><th class="num">#</th><th>艺人 A</th><th>艺人 B</th><th class="num">合作次数</th></tr></thead>
          <tbody id="t-copair"></tbody></table>
        </div>
      </div>
      <div class="card"><h3>演出人数分布</h3><div id="c-acount"></div></div>
    </div>
  </section>

  <section id="s-org">
    <h2>片商 / 系列 / 导演</h2>
    <p class="hint">片商与系列取自 studio / maker / series 字段及「片商:」「系列:」前缀标签</p>
    <div class="grid g2">
      <div class="card"><h3>片商 Top 20</h3><div id="c-studios"></div></div>
      <div class="card"><h3>系列 Top 20</h3><div id="c-series"></div></div>
      <div class="card"><h3>导演 Top 20</h3><div id="c-directors"></div></div>
      <div class="card"><h3>番号前缀 Top 20 <small>反映收录的厂商系列线</small></h3><div id="c-prefixes"></div></div>
    </div>
  </section>

  <section id="s-tech">
    <h2>画质与规格</h2>
    <div class="grid g3">
      <div class="card"><h3>分辨率分布</h3><div id="c-res"></div></div>
      <div class="card"><h3>有码 / 无码</h3><div id="c-censor"></div></div>
      <div class="card"><h3>判定说明</h3>
        <p class="hint" style="margin:0 0 10px">由标签与标题中的「有码 / 无码 / 無修正 / UNCENSORED / モザイク」等关键词判定，
        无法判定时归入「未知」。</p>
        <div class="chips" id="c-censor-tip"></div>
      </div>
    </div>
  </section>

  <section id="s-time">
    <h2>时间维度</h2>
    <p class="hint">发行年份取自 premiered / release；入库月份与时段取自 dateadded，反映实际收藏节奏</p>
    <div class="grid g2">
      <div class="card"><h3>发行年份趋势</h3><div id="c-year"></div></div>
      <div class="card"><h3>入库月份趋势</h3><div id="c-month"></div></div>
      <div class="card"><h3>入库时段分布 <small>24 小时</small></h3><div id="c-hour"></div></div>
      <div class="card"><h3>入库星期分布</h3><div id="c-week"></div></div>
    </div>
  </section>

  <section id="s-rate">
    <h2>评分与时长</h2>
    <div class="grid g2">
      <div class="card"><h3>评分分布 <small>仅统计已评分作品</small></h3><div id="c-rating"></div></div>
      <div class="card"><h3>时长分布 <small>单位：分钟</small></h3><div id="c-runtime"></div></div>
    </div>
  </section>

  <section id="s-cooc">
    <h2>标签共现</h2>
    <p class="hint">同一部作品里同时出现的标签对，对角线为该标签自身出现次数；颜色越深共现越强</p>
    <div class="card" style="margin-bottom:16px"><h3>共现矩阵 <small>Top 36 标签</small></h3><div id="c-heat"></div></div>
    <div class="card"><h3>最强共现组合 Top 25</h3>
      <div class="scroll" style="max-height:340px">
        <table><thead><tr><th class="num">#</th><th>标签 A</th><th>标签 B</th><th class="num">共现次数</th></tr></thead>
        <tbody id="t-pairs"></tbody></table>
      </div>
    </div>
  </section>

  <section id="s-kw">
    <h2>剧情高频词</h2>
    <p class="hint">无词典新词发现：凝固度 + 左右邻字熵 + 扩展度三重检验，无需 jieba 等分词库</p>
    <div class="grid g2">
      <div class="card"><h3>词云</h3><div id="c-kwcloud"></div></div>
      <div class="card"><h3>词频 Top 60</h3><div id="c-kwbar"></div></div>
    </div>
  </section>

  <section id="s-detail">
    <h2>作品明细<span class="tip">i<span class="tiptext">作品明细：报告内嵌前 <b>__MOVIES__</b> 条作品（按评分降序），便于在单文件报告里快速浏览。完整字段与全部记录请用左侧「导出」生成 CSV / XLSX / JSON，或命令行导出。</span></span></h2>
    <p class="hint">报告内嵌前 __MOVIES__ 条（按评分排序）；完整明细请用命令行导出 CSV / XLSX</p>
    <div class="card"><div id="t-movies"></div></div>
  </section>

  <footer>
    __APP__ v__VER__ · 生成于 __GEN__ · 单文件离线报告，可直接分享<br>
    Copyright &copy; 2026 肆月Aperture · 本软件不得用于商业用途，仅做学习交流使用。
  </footer>
</div>

<script>window.__DATA__ = __PAYLOAD__;</script>
<script>__JS__</script>
</body>
</html>"""


def build_html(
    data: Dict[str, Any],
    *,
    title: Optional[str] = None,
    movies: Optional[List[Dict[str, Any]]] = None,
    source_label: str = "",
    top_tags: int = 40,
    top_actors: int = 30,
) -> str:
    """生成单文件 HTML 报告字符串。"""
    payload = dict(data)
    payload["movies"] = movies or []
    payload["meta"] = {"app": APP_NAME, "version": __version__, "source": source_label}

    total = (data.get("overview") or {}).get("total", 0)
    title = title or f"{APP_NAME} · 用户×癖分析报告"
    sub_parts = []
    if source_label:
        sub_parts.append(f"数据源：{source_label}")
    sub_parts.append(f"共 {total:,} 部作品")
    sub_parts.append(f"生成于 {data.get('generated_at', '')}")

    # 注意：__PAYLOAD__ 必须是独立占位符，不能用 __DATA__——
    # 否则 JS 里的 `window.__DATA__` 会被一并替换，导致整个脚本报废。
    return (_TEMPLATE
            .replace("__CSS__", _CSS)
            .replace("__JS__", _JS)
            .replace("__PAYLOAD__", _json_for_js(payload))
            .replace("__TITLE__", _html.escape(title))
            .replace("__SUB__", _html.escape(" · ".join(sub_parts)))
            .replace("__TOTAL__", f"{total:,}")
            .replace("__MOVIES__", f"{len(payload['movies']):,}")
            .replace("__TOP_TAGS__", str(top_tags))
            .replace("__TOP_ACTORS__", str(top_actors))
            .replace("__APP__", APP_NAME)
            .replace("__VER__", __version__)
            .replace("__GEN__", data.get("generated_at", "")))


def write_html_report(
    data: Dict[str, Any],
    out_path: str,
    *,
    title: Optional[str] = None,
    movies: Optional[List[Dict[str, Any]]] = None,
    source_label: str = "",
    top_tags: int = 40,
    top_actors: int = 30,
) -> str:
    """生成并写入 HTML 报告，返回文件路径。"""
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    text = build_html(
        data, title=title, movies=movies, source_label=source_label,
        top_tags=top_tags, top_actors=top_actors,
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out_path


__all__ = ["build_html", "write_html_report"]
